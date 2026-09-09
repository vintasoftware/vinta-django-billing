from typing import TYPE_CHECKING, Any, ClassVar

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, UniqueConstraint

from vinta_billing import conf
from vinta_billing.base_models import BaseModel
from vinta_billing.constants import (
    BillingInterval,
    BillingState,
    DocumentTypes,
    LimitKind,
    LimitWarningLevel,
    PaymentProviders,
    PaymentStatuses,
    ProviderWebhookRoute,
    RefundStatuses,
    ScopeType,
    SubscriptionStatuses,
)
from vinta_billing.managers import (
    BillingPeriodSummaryManager,
    BillingScopeManager,
    LimitWarningNotificationManager,
    MeteredOccurrenceManager,
    ProviderWebhookEventManager,
)
from vinta_billing.registry import entitlement_choices, resource_choices, resources


if TYPE_CHECKING:
    from django_stubs_ext.db.models.manager import RelatedManager


#: The model every scope foreign key in this app points at, resolved at import
#: time because a field definition needs a target now. Defaults to the model
#: this app ships, so an installation that has not overridden it still works --
#: the swappable machinery reads the same setting and simply finds nothing to
#: swap.
SCOPE_MODEL = conf.scope_model_string()


class AbstractBillingScope(BaseModel):
    """Who is being billed.

    Every table in this app hangs off one of these rows. The indirection is the
    point: a foreign key straight to a project's tenant model resolves to
    exactly one model per project, so a project could sell a plan to an
    scope *or* to a user but never to both. A scope row can be either,
    and ``scope_type`` says which.

    Subclasses decide what a scope *is* by implementing :meth:`build_scope_key`
    and the ``scope`` property over whatever columns suit them -- a generic key,
    a nullable foreign key per kind, a composite. This class owns what every
    such choice has in common: a portable string spelling of the value, a
    display label, an owner, and a place in a hierarchy.

    Three columns exist so that the shipped defaults work with no project code
    at all:

    ``label``
        The display name, live rather than snapshotted -- a payer is a thing
        that still exists. Replaces every ``organization.name`` read this
        package used to do, and stops it assuming the payer has a ``name``.
    ``owner``
        Who may change this scope's billing and who hears when a charge fails,
        under the shipped :func:`~vinta_billing.permissions.owner_may_manage_billing`
        and :func:`~vinta_billing.recipients.scope_owner` defaults. ``SET_NULL``:
        deleting a user must not delete their payment history.
    ``parent``
        A reseller chain, on a table this package owns, so
        :class:`~vinta_billing.hierarchy.ParentFieldHierarchy` needs no field on
        a model it does not control.

    A project that swaps the scope model out and populates none of the three
    pays one NULL each.
    """

    # One of ``ScopeType``, or a value the installing project defines. No
    # ``choices``: see ``ScopeType``.
    scope_type = models.CharField(max_length=32, default=ScopeType.ORGANIZATION)

    # The portable spelling of the scope, maintained by ``save``. Stable for the
    # life of the scope and unique among scopes of the same type: it is the
    # idempotency key provisioning code reaches for and the handle the admin and
    # the API address a scope by, so a key that changes strands both.
    scope_key = models.CharField(max_length=255, db_index=True)

    label = models.CharField(max_length=255, blank=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    # ``PROTECT``, not ``CASCADE``: deleting a reseller must not silently delete
    # every subscription underneath it. ``%(class)s`` in the related name
    # because more than one concrete scope model can be *defined* in a project
    # even though only one is ever active, and two bare ``children`` accessors
    # on one target would clash.
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_children",
    )

    class Meta(BaseModel.Meta):
        abstract = True

    def __str__(self) -> str:
        return self.label or self.scope_key or str(self.pk)

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.validate_scope()
        self.scope_key = self.build_scope_key()
        if (
            update_fields := kwargs.get("update_fields")
        ) is not None and "scope_key" not in update_fields:
            # A partial update that moves the scope but leaves ``scope_key``
            # behind would silently detach the scope from every billing row
            # that found it by key, so add the column rather than let the write
            # proceed.
            kwargs["update_fields"] = [*update_fields, "scope_key"]
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        self.validate_scope()

    @property
    def scope(self) -> Any:
        """The thing being billed: a user, an scope, whatever."""
        raise NotImplementedError("Needs to be implemented on subclass")

    @scope.setter
    def scope(self, value: Any) -> None:
        raise NotImplementedError("Needs to be implemented on subclass")

    def build_scope_key(self) -> str:
        """Return the portable string form of this scope.

        Must be stable for the life of the scope and unique among scopes of the
        same ``scope_type``.
        """
        raise NotImplementedError("Needs to be implemented on subclass")

    def validate_scope(self) -> None:
        """Reject a scope that names nothing.

        Unlike an audit scope, a billing scope has no "global" value: somebody
        pays. A convenience check rather than the guarantee -- ``save`` is
        bypassed by ``bulk_create`` and ``QuerySet.update``, so concrete
        subclasses are expected to carry a CHECK constraint saying the same
        thing.
        """
        if not self.build_scope_key():
            raise ValidationError("A billing scope must name something to bill.")


class BillingScope(AbstractBillingScope):
    """The scope model this app ships: a generic key to anything.

    A ``content_type``/``object_id`` pair rather than the opaque string
    ``vinta-django-audit-logs`` uses for its scope, and the divergence is
    deliberate. An audit scope is written on an append-only hot path where the
    join is unaffordable; a billing scope is read about once per request. Paying
    for the generic key buys the thing this model exists for -- both kinds of
    payer, in one project, with no project code::

        BillingScope.objects.get_or_create_for(request.user)          # personal
        BillingScope.objects.get_or_create_for(request.scope)  # team

    A project that wants real referential integrity, typed access and CHECK
    constraints per kind subclasses :class:`AbstractBillingScope` with named
    foreign keys instead and points ``BILLING_SCOPE_MODEL`` at that.

    ``content_type`` is ``PROTECT``: ``remove_stale_contenttypes`` runs after
    every migrate that drops a model, and a scope whose content type vanished
    could no longer name what it bills. The *target row* is a different matter
    -- nothing constrains it, so deleting an scope leaves the scope, its
    label and its payment history intact, which is what an auditable billing
    trail needs.
    """

    content_type = models.ForeignKey(
        "contenttypes.ContentType",
        on_delete=models.PROTECT,
        related_name="+",
    )
    # A string, so it holds an integer pk, a UUID or a natural key equally well,
    # and so the scope does not change shape when the payer model does.
    object_id = models.CharField(max_length=255)
    scope_object = GenericForeignKey("content_type", "object_id")

    objects: ClassVar[BillingScopeManager] = BillingScopeManager()

    class Meta(AbstractBillingScope.Meta):
        abstract = False
        swappable = "BILLING_SCOPE_MODEL"
        constraints: ClassVar = [
            # The invariant ``validate_scope`` checks, held where ``save``
            # cannot reach: ``bulk_create`` and ``QuerySet.update`` never call
            # it.
            models.CheckConstraint(
                condition=~Q(object_id=""),
                name="billing_scope_names_a_payer",
            ),
            # ``scope_key`` is what provisioning code looks a scope up by, so it
            # is the half that has to be unique. ``label`` is a display value
            # and must stay free to change.
            UniqueConstraint(
                fields=["scope_type", "scope_key"],
                name="billing_scope_unique_key_per_type",
            ),
        ]

    @property
    def scope(self) -> Any:
        return self.scope_object

    @scope.setter
    def scope(self, value: Any) -> None:
        self.scope_object = value

    def build_scope_key(self) -> str:
        """``"app_label.modelname:pk"``.

        Readable in an export and meaningful without a join, which a bare
        ``content_type_id`` is not -- content type ids differ between databases.
        """
        if self.content_type_id is None or not self.object_id:
            return ""
        content_type = self.content_type
        return f"{content_type.app_label}.{content_type.model}:{self.object_id}"


class BillingAddress(BaseModel):
    street_name = models.TextField()
    street_number = models.TextField()
    neighborhood = models.TextField(blank=True)
    address_line_2 = models.TextField(blank=True)
    city = models.CharField(max_length=255)
    state = models.CharField(max_length=255)
    country = models.CharField(max_length=255)
    zip_code = models.CharField(max_length=10)

    billing_profile: "BillingProfile"

    def __str__(self):
        return (
            f"{self.id} {self.scope} - {self.city} - {self.state} - "
            f"{self.country} - {self.zip_code}"
        )

    @property
    def scope(self):
        return getattr(self, "billing_profile", None) and self.billing_profile.scope


class BillingPlan(BaseModel):
    """Catalog plan that a ``Subscription`` is sold against.

    Carries its ``PlanLimit`` / ``PlanEntitlement`` rows (the plan catalog proper).
    There is no feature flag for the limits/entitlements rollout: the ``unlimited``
    plan — every ``PlanLimit.limit_value`` NULL, every ``PlanEntitlement`` enabled —
    *is* the kill switch. Catalog edits here never propagate to an already-sold
    subscription; see ``SubscriptionPlanLimit`` for the per-subscription copy.
    """

    slug = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True, db_index=True)
    is_default_for_new_scopes = models.BooleanField(default=False)
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2)
    annual_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3)
    grace_period_days = models.PositiveIntegerField(null=True, blank=True)

    subscriptions: "RelatedManager[Subscription]"
    limits: "RelatedManager[PlanLimit]"
    entitlements: "RelatedManager[PlanEntitlement]"

    #: Per-instance opt-out of the limit-coverage check in ``clean`` — set by
    #: ``BillingPlanAdmin``'s form, which validates coverage on the inline formset
    #: instead (the only place that can see the rows the save is about to write).
    skip_limit_coverage_validation: bool = False

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["is_default_for_new_scopes"],
                condition=Q(is_default_for_new_scopes=True),
                name="uniq_default_billing_plan",
            )
        ]

    def __str__(self):
        return self.name

    def get_missing_limited_resource_keys(self) -> list[str]:
        """Registered resources this plan carries no ``PlanLimit`` row for.

        A plan is *complete* when this is empty. Completeness is an invariant, not
        a preference: an absent ``PlanLimit`` row (or a stale
        ``SubscriptionPlanLimit`` left over from a previous plan with
        ``limit_value=None``) reads as **unlimited** in ``EntitlementService``, so
        an incomplete plan silently grants an infinite ceiling on the resource it
        omits. "Not included" is expressed with ``limit_value=0``, never omission.

        An unsaved plan has no rows to read (the related manager raises on an
        instance with no pk), so it is reported as missing everything — which is
        what ``clean`` should say about it.
        """
        expected = set(resources.keys())
        if self.pk is None:
            return sorted(expected)
        covered = set(self.limits.values_list("resource_key", flat=True))
        return sorted(expected - covered)

    def clean(self) -> None:
        """Reject an incomplete plan at authoring time rather than at downgrade time.

        ``BillingPlanAdmin`` skips this one check (see
        ``BillingPlanAdmin.form``) because the parent form is validated *before*
        its ``PlanLimit`` inline formset is saved — the rows that would make the
        plan complete are still pending, so this would reject the very edit that
        fixes it, with no way out. The admin runs the equivalent check on the
        inline formset instead, against the rows the save is about to produce.
        """
        super().clean()
        if self.skip_limit_coverage_validation:
            return
        missing = self.get_missing_limited_resource_keys()
        if missing:
            raise ValidationError(
                {
                    "__all__": (
                        f"This plan has no PlanLimit row for {missing}. Every plan must "
                        "carry a row for every limited resource — 'not included' is "
                        "limit_value=0, never omission, because an omitted row reads as "
                        "unlimited."
                    )
                }
            )


class PlanLimit(BaseModel):
    """A single resource ceiling on a ``BillingPlan``.

    ``limit_value=NULL`` means no ceiling (unlimited) — never treat NULL as zero.
    ``kind`` mirrors the resource registry's own prepaid/postpaid split so an
    effective-limit resolution does not have to cross-reference the choices class.
    """

    plan = models.ForeignKey(BillingPlan, on_delete=models.CASCADE, related_name="limits")
    resource_key = models.CharField(max_length=100, choices=resource_choices)
    limit_value = models.PositiveIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=20, choices=LimitKind)
    overage_unit_price = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["plan", "resource_key"],
                name="uniq_plan_limit_resource",
            )
        ]

    def __str__(self):
        return f"{self.plan} - {self.resource_key} - {self.limit_value}"


class PlanEntitlement(BaseModel):
    """A single boolean feature gate on a ``BillingPlan``."""

    plan = models.ForeignKey(BillingPlan, on_delete=models.CASCADE, related_name="entitlements")
    entitlement_key = models.CharField(max_length=100, choices=entitlement_choices)
    is_enabled = models.BooleanField(default=False)

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["plan", "entitlement_key"],
                name="uniq_plan_entitlement_key",
            )
        ]

    def __str__(self):
        return f"{self.plan} - {self.entitlement_key} - {self.is_enabled}"


class BillingProfile(BaseModel):
    # A surrogate primary key, where this used to *be* its payer
    # (``organization`` was ``primary_key=True``). Two reasons it changed with
    # the move to scopes. A profile's identity should not shift when its payer
    # is re-scoped -- re-pointing a profile at a different scope would
    # otherwise rewrite its pk and every ``Payment`` row hanging off it. And
    # the values in that column are what ``Payment.billing_profile_id`` already
    # holds, so keeping them under a surrogate ``id`` is what lets the upgrade
    # leave dependent rows untouched; see migration 0006.
    scope = models.OneToOneField(
        SCOPE_MODEL,
        on_delete=models.CASCADE,
        related_name="billing_profile",
    )
    # Payer identity sent to the payment gateway. Distinct from the future
    # a membership's `is_billing_owner`, which is about who may *manage*
    # billing — these fields are about what the gateway needs to charge the
    # scope (e.g. MercadoPago rejects a payer with no email).
    contact_first_name = models.CharField(max_length=255)
    contact_last_name = models.CharField(max_length=255, blank=True)
    contact_email = models.EmailField()
    contact_phone = models.CharField(max_length=50, blank=True)
    document_type = models.CharField(max_length=50, choices=DocumentTypes)
    document_number = models.CharField(max_length=50)
    billing_address = models.OneToOneField(
        BillingAddress, on_delete=models.CASCADE, related_name="billing_profile"
    )
    #: The payment provider this scope is pinned to, written once by
    #: ``SubscriptionService.record_payment_method`` when the scope's
    #: first payment instrument is confirmed. Null means "never paid" and
    #: resolves to ``VINTA_BILLING['DEFAULT_PROVIDER']``. Once set, every new
    #: charge and subscription for this scope goes through this
    #: provider -- the instrument on file lives there and nowhere else.
    #: Repointing is a staff action (``SubscriptionService.set_payment_provider``),
    #: not something any API surface exposes.
    payment_provider = models.CharField(
        max_length=50, choices=PaymentProviders, blank=True, default=""
    )

    def __str__(self):
        return f"{self.pk} {self.scope} - {self.document_type} - {self.document_number}"


class Subscription(BaseModel):
    """An scope's subscription to a ``BillingPlan``.

    Two status concepts coexist here and share member names (``active``,
    ``cancelled``, ``pending``) — do not conflate them:

    - ``status`` (``SubscriptionStatuses``) mirrors the provider-reported state of
      the subscription, fed by ``SubscriptionStatusUpdate`` rows as the gateway
      reports them (e.g. MercadoPago's ``authorized`` / ``paused`` / ``cancelled``).
    - ``billing_state`` (``BillingState``) is this app's internal billing
      lifecycle (free / active / grace / restricted / cancelled) used to gate
      access. It is derived from, but not identical to, ``status``.
    """

    scope = models.OneToOneField(SCOPE_MODEL, on_delete=models.CASCADE, related_name="subscription")
    plan = models.ForeignKey(BillingPlan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(
        max_length=50, choices=SubscriptionStatuses, default=SubscriptionStatuses.PENDING_SEND
    )
    billing_state = models.CharField(
        max_length=20, choices=BillingState, default=BillingState.FREE, db_index=True
    )
    billing_interval = models.CharField(
        max_length=10, choices=BillingInterval, default=BillingInterval.MONTHLY
    )
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField(db_index=True)
    grace_period_ends_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # The last time `DunningService`'s beat task (`process_dunning`) retried the
    # charge / sent that day's rung of the dunning ladder for this subscription.
    # This is the per-attempt idempotency check that keeps an at-least-once job
    # redelivery or an accidental double beat-tick from
    # double-charging the provider for the same logical attempt or double-sending
    # that attempt's notification. Provider-side idempotency (the retry charge's
    # `idempotency_key`) is what makes the charge itself safe either way; this is
    # what keeps the notification side just as safe. Cleared whenever the
    # subscription leaves GRACE, so a later re-entry starts its own ladder fresh.
    last_dunning_attempt_at = models.DateTimeField(null=True, blank=True)
    external_id = models.CharField(max_length=255, blank=True, db_index=True)
    plan_external_id = models.CharField(max_length=255, blank=True)
    payment_provider = models.CharField(max_length=50, choices=PaymentProviders)

    # A downgrade takes no cash refund and does not touch `plan` (or the price the
    # org is billed) until the next period boundary — only the lower
    # `SubscriptionPlanLimit`/`SubscriptionEntitlement` rows apply immediately (see
    # `SubscriptionService._schedule_downgrade`). These three fields record that a
    # flip is owed and to what; the cycle-close sweep is what applies it at the
    # next boundary, so a scheduled downgrade sits here until that sweep reads it.
    # `pending_plan` doubles as the marker: `None` means "no plan change scheduled".
    pending_plan = models.ForeignKey(
        BillingPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    pending_billing_interval = models.CharField(max_length=10, choices=BillingInterval, blank=True)
    pending_plan_effective_at = models.DateTimeField(null=True, blank=True)

    # Set when `_initiate_upgrade` has driven the provider for an upgrade whose
    # charge has not yet been confirmed by a subscription-payment webhook, and
    # cleared by `confirm_plan_change` once it is. Prevents a second upgrade being
    # initiated to a different plan before the first confirms — otherwise the first
    # webhook would grant whatever plan `subscription.plan` currently points at
    # (the latest requested tier), not the plan its charge actually paid for (see
    # `SubscriptionService.request_plan_change`).
    plan_change_pending_confirmation = models.BooleanField(default=False)

    limits: "RelatedManager[SubscriptionPlanLimit]"
    entitlements: "RelatedManager[SubscriptionEntitlement]"
    add_ons: "RelatedManager[SubscriptionAddOn]"
    payments: "RelatedManager[Payment]"

    class Meta(BaseModel.Meta):
        # Declared on ``Subscription`` rather than on the scope model --
        # which this package does not own and cannot add a permission to -- and
        # because the subscription is the object the capability acts on: changing
        # the plan, buying add-ons, managing the payment method.
        #
        # Nothing here grants it and nothing here requires it. It exists so a
        # project that expresses roles as ``vinta-django-orgs`` scope
        # permissions has a codename to grant, and so the two seams that ask "who
        # may manage billing" (``BILLING_MANAGER_PREDICATE``) and "who is told
        # about it" (``BILLING_RECIPIENTS``) can be answered from one grant --
        # see ``vinta_billing.permissions.member_holding_manage_billing`` and
        # ``vinta_billing.recipients.members_holding_manage_billing``, neither of
        # which is the default.
        permissions: ClassVar = [
            ("manage_billing", "Can manage the scope's billing"),
        ]

    def __str__(self):
        return (
            f"{self.id} - {self.status} - {self.current_period_start} - {self.current_period_end}"
        )


class PaymentMethod(BaseModel):
    """A payment instrument on file for an scope's billing root.

    The real record ``EntitlementService.has_payment_method`` points at, replacing
    the earlier ``Subscription.billing_state`` allow-list proxy that was used
    before any instrument actually existed (see that method's docstring for the
    full history). Deliberately decoupled from ``billing_state``: an scope
    can be ``ACTIVE`` from a past cycle with no current instrument on file (e.g.
    after an admin edit), or have a valid card on file while ``GRACE`` (a failed
    *charge* moves ``ACTIVE -> GRACE``, which says nothing about whether the card
    itself is still attached) — querying this table is what makes those cases
    resolve correctly instead of by inference from state.

    Written only on **confirmed** evidence that the provider accepted the
    instrument: the subscription-payment and payment webhook paths
    (``PaymentsViewSet``) call ``SubscriptionService.record_payment_method`` once a
    charge against it is reported ``APPROVED`` — never synchronously from the
    request that merely *attempts* to attach one. Not tenant-scoped
    (no scope-filtering default manager) for the same reason as the other models in this
    module: cross-scope billing reads would otherwise force an
    ``original_manager`` escape at nearly every call site.
    """

    scope = models.ForeignKey(SCOPE_MODEL, on_delete=models.CASCADE, related_name="payment_methods")
    provider = models.CharField(max_length=50, choices=PaymentProviders)
    external_id = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["scope", "provider", "external_id"],
                name="uniq_payment_method",
            )
        ]

    def __str__(self):
        return f"{self.scope_id} - {self.provider} - {self.external_id}"


class SubscriptionPlanLimit(BaseModel):
    """Per-subscription copy of a ``PlanLimit`` row — the support lever.

    Copied from the catalog ``PlanLimit`` on subscription creation and re-copied on
    plan change (``SubscriptionService.change_plan``). Catalog edits to ``PlanLimit``
    never propagate here — an scope keeps what it was sold, and a catalog typo
    cannot silently lower limits for every subscriber at once.

    ``is_overridden=True`` marks a row an admin edited by hand in Django admin (see
    ``vinta_billing/admin.py``'s ``SubscriptionPlanLimitInline``) — this is the support
    lever for a stuck scope, and it is why there is no support-facing
    enforcement bypass elsewhere. A plan change re-copies every non-overridden row
    from the new plan's ``PlanLimit`` set and leaves ``is_overridden=True`` rows
    untouched.
    """

    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="limits")
    resource_key = models.CharField(max_length=100, choices=resource_choices)
    limit_value = models.PositiveIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=20, choices=LimitKind)
    overage_unit_price = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    is_overridden = models.BooleanField(default=False)

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["subscription", "resource_key"],
                name="uniq_sub_limit_resource",
            )
        ]

    def __str__(self):
        return f"{self.subscription} - {self.resource_key} - {self.limit_value}"


class SubscriptionEntitlement(BaseModel):
    """Per-subscription copy of a ``PlanEntitlement`` row.

    Mirrors ``SubscriptionPlanLimit``'s override semantics: catalog edits do not
    propagate, and ``is_overridden=True`` rows survive a plan change untouched.
    """

    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="entitlements"
    )
    entitlement_key = models.CharField(max_length=100, choices=entitlement_choices)
    is_enabled = models.BooleanField(default=False)
    is_overridden = models.BooleanField(default=False)

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["subscription", "entitlement_key"],
                name="uniq_sub_entitlement_key",
            )
        ]

    def __str__(self):
        return f"{self.subscription} - {self.entitlement_key} - {self.is_enabled}"


class SubscriptionAddOn(BaseModel):
    """Extra capacity bought on top of a ``Subscription``'s plan limits.

    An active add-on's ``quantity`` is added to the matching
    ``SubscriptionPlanLimit.limit_value`` when resolving the effective ceiling
    (``EntitlementService.get_effective_limit``). An add-on on a resource whose
    limit is NULL (unlimited) changes nothing — unlimited plus anything is still
    unlimited.

    ``purchase_idempotency_key`` is unique so a retried purchase (a double-clicked
    button, a job re-delivered under at-least-once delivery) neither
    grants capacity twice nor charges twice: ``SubscriptionService.purchase_add_on``
    ``get_or_create``s on this field before doing anything else, so the same key
    posted twice always resolves to the same row and the provider is only ever
    charged once — see that method's docstring for the fail-closed-for-money
    reasoning.

    ``is_active`` starts ``False`` and is flipped by
    ``SubscriptionService.activate_add_on``, called from the webhook path once
    ``payment`` is confirmed ``APPROVED`` — never synchronously at purchase time.
    ``EntitlementService.get_effective_limit`` only sums ``is_active=True`` rows,
    so an initiated-but-unconfirmed purchase grants no capacity.
    """

    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="add_ons")
    resource_key = models.CharField(max_length=100, choices=resource_choices)
    quantity = models.PositiveIntegerField()
    is_recurring = models.BooleanField()
    is_active = models.BooleanField(default=True, db_index=True)
    external_id = models.CharField(max_length=255, blank=True)
    purchase_idempotency_key = models.CharField(max_length=255, unique=True)
    # Nullable: only set once `purchase_add_on` drives the one-time charge (it is
    # not set at all for an add-on granted some other way, e.g. a support override
    # made directly in admin). `on_delete=SET_NULL` rather than `CASCADE` -- a
    # `Payment` row is a financial record and must survive independently of the
    # add-on it happened to fund.
    payment = models.OneToOneField(
        "vinta_billing.Payment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="add_on",
    )

    def __str__(self):
        return f"{self.subscription} - {self.resource_key} - +{self.quantity}"


class Payment(BaseModel):
    value = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=50)
    payment_provider = models.CharField(max_length=50, choices=PaymentProviders)
    external_id = models.CharField(max_length=255)
    status = models.CharField(max_length=50, choices=PaymentStatuses)
    original_status = models.CharField(max_length=50)
    billing_profile = models.ForeignKey(
        BillingProfile, on_delete=models.CASCADE, related_name="billing"
    )
    payment_method = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="billing", null=True, blank=True
    )

    status_updates: "RelatedManager[PaymentStatusUpdate]"

    def __str__(self):
        return (
            f"{self.id} {self.scope} - {self.value} - "
            f"{self.payment_provider} - {self.status} - {self.created.isoformat()}"
        )

    @property
    def scope(self):
        return getattr(self, "billing_profile", None) and self.billing_profile.scope


class Refund(BaseModel):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="refunds")
    value = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=50)
    external_id = models.CharField(max_length=255)
    status = models.CharField(
        max_length=50, choices=RefundStatuses, default=RefundStatuses.PENDING_SEND
    )

    def __str__(self):
        return (
            f"{self.id} {self.payment} - {self.value} - {self.currency} - "
            f"{self.created.isoformat()}"
        )


class PaymentStatusUpdate(BaseModel):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="status_updates")
    status = models.CharField(max_length=50, choices=PaymentStatuses)
    description = models.TextField(blank=True)
    external_id = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.id} {self.payment} - {self.status} - {self.created.isoformat()}"


class SubscriptionStatusUpdate(BaseModel):
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="status_updates"
    )
    status = models.CharField(max_length=50, choices=SubscriptionStatuses)
    description = models.TextField(blank=True)
    external_id = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.id} {self.subscription} - {self.status} - {self.created.isoformat()}"


class RefundStatusUpdate(BaseModel):
    refund = models.ForeignKey(Refund, on_delete=models.CASCADE, related_name="status_updates")
    status = models.CharField(max_length=50, choices=RefundStatuses)
    description = models.TextField(blank=True)
    external_id = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.id} {self.refund} - {self.status} - {self.created.isoformat()}"


class ProviderWebhookEvent(BaseModel):
    """Idempotency ledger for inbound payment-provider webhook notifications.

    Not tenant-scoped: a webhook notification arrives before we know which
    scope it resolves to (see the billing plans and limits plan's Data Model
    Changes — cross-scope billing reads are the reason these models stay
    plain FK, no scope-filtering manager). ``(provider, route,
    external_event_id)`` uniquely identifies one delivery attempt at the provider;
    ``processed_at`` is set only once the corresponding domain update
    (payment/subscription status) has actually been applied, so a row that exists
    with ``processed_at=None`` means a previous delivery was recorded but crashed
    before finishing — the next delivery for the same event is allowed to retry
    rather than being silently dropped.
    """

    provider = models.CharField(max_length=50, choices=PaymentProviders)
    route = models.CharField(max_length=50, choices=ProviderWebhookRoute)
    external_event_id = models.CharField(max_length=255)
    payload = models.JSONField(default=dict, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects: ClassVar[ProviderWebhookEventManager] = ProviderWebhookEventManager()

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["provider", "route", "external_event_id"],
                name="uniq_provider_webhook_event",
            )
        ]

    def __str__(self):
        return f"{self.provider} - {self.route} - {self.external_event_id}"


class MeteredOccurrence(BaseModel):
    """One event occurrence, recorded as billable exactly once, ever.

    Occurrences of a recurring series are *computed* in Postgres, never stored
    (``calculate_recurring_events`` and friends), so there is no row to bill
    against. This table is that row — written by ``MeteringService`` from a
    scheduled sweep of elapsed time.

    **The unique constraint is the correctness mechanism, not the code path.**
    ``(scope, event_id, occurrence_start)`` plus
    ``bulk_create(..., ignore_conflicts=True)`` is what makes re-running a window,
    or running two windows that overlap, harmless. The sweep window deliberately
    overlaps the previous one so that a missed run self-heals on the next pass;
    that is only safe because a re-insert is a no-op at the database level rather
    than something application code has to remember to check. Do not replace it
    with an application-level "have I already seen this?" lookup — that lookup
    races itself, and the failure is a silent wrong number on an invoice rather
    than an exception.

    ``event_id`` is a soft reference (``BigIntegerField``, not a ``ForeignKey``) on
    purpose: deleting an event must not delete the record that its occurrences were
    billed. An occurrence is billed at most once *ever*, and that fact has to
    outlive the event.

    What the two identity columns hold, precisely. ``event_id`` is the **series
    root** — the original master, following ``bulk_modification_parent`` back
    through any splits — so moving later occurrences onto a continuation row does
    not make already-billed occurrences look new. ``occurrence_start`` is the
    occurrence's **current start time**, nothing cleverer: the expansion has no
    notion of a "slot" distinct from ``start_time`` to key on
    (``calculate_recurring_events`` emits a modified exception as the moved row's
    own ``me.start_time``). Re-timing an occurrence therefore mints a new identity
    and bills it again — a known, deferred defect, characterised in
    the reconciliation tests and reported by
    ``MeteringService.reconcile_period`` as ``orphaned`` drift.

    ``is_within_allowance`` and ``unit_price`` are stamped **at meter time** against
    the allowance and overage price in force at that moment, so a later plan change
    or limit override cannot retroactively reprice usage that already happened.

    No scope-filtering default manager: billing legitimately reads across scopes
    (a reseller root's cycle close sums its whole subtree), and the tenant-safe
    queryset layer would force an ``original_manager`` escape at nearly every call
    site. The ``scope`` FK is still present and every read goes through
    ``MeteredOccurrenceQuerySet.for_scopes``.
    """

    scope = models.ForeignKey(
        SCOPE_MODEL,
        on_delete=models.CASCADE,
        related_name="metered_occurrences",
    )
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="metered_occurrences"
    )
    event_id = models.BigIntegerField()
    occurrence_start = models.DateTimeField()
    billing_period_start = models.DateTimeField(db_index=True)
    is_within_allowance = models.BooleanField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=4)

    objects: ClassVar[MeteredOccurrenceManager] = MeteredOccurrenceManager()

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["scope", "event_id", "occurrence_start"],
                name="uniq_metered_occurrence",
            )
        ]
        indexes: ClassVar = [
            models.Index(
                fields=["subscription", "billing_period_start"],
                name="metered_occ_sub_period_idx",
            )
        ]

    def __str__(self):
        return f"{self.scope_id}/{self.event_id} @ {self.occurrence_start.isoformat()}"


class LimitWarningNotification(BaseModel):
    """Durable idempotency marker for the approaching-limit / limit-reached
    in-app notification (the ``check_approaching_limits`` beat task).

    **The unique constraint is the correctness mechanism, not the code path** --
    the same pattern ``MeteredOccurrence`` and ``ProviderWebhookEvent`` use
    elsewhere in this app. ``check_approaching_limits`` re-checks every
    subscription on every beat tick; without a durable marker it would re-send
    the same warning every tick for as long as usage stays above the
    threshold. ``UsageWarningService.check_subscription`` claims the marker
    with ``get_or_create`` and sends inside the same ``transaction.atomic()``
    block -- the row existing after that transaction commits is the single
    source of truth for "have we already told this scope about this?",
    not an in-memory flag, which would not survive a beat task being retried
    on a different worker. If the send raises, the transaction rolls back and
    un-claims the marker, so a transient failure is retried on the next beat
    tick within the same cycle rather than being silently debounced for a
    notification that never went out.

    ``billing_period_start`` (``current_billing_period_start`` --
    ``vinta_billing.services.subscription_service``, the same function the
    ``event_occurrences`` usage counter and the meter both anchor on) is the
    "cycle" the debounce resets on: once a cycle rolls over, usage sitting
    above the threshold across the boundary is allowed to warn again, rather
    than being permanently silenced by a marker from a prior cycle. For a
    pre-paid resource (no natural billing-period semantics of its own) this
    still ties the debounce to the one cycle notion every other billing-cycle
    computation shares, rather than inventing a second one.

    ``level`` (``LimitWarningLevel``) keeps "approaching" and "reached" as two
    independently debounced markers, so crossing 80% and later crossing 100%
    in the same cycle both notify exactly once each, rather than the second
    crossing being silently swallowed by the first marker.
    """

    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="limit_warnings"
    )
    resource_key = models.CharField(max_length=100, choices=resource_choices)
    billing_period_start = models.DateTimeField(db_index=True)
    level = models.CharField(max_length=20, choices=LimitWarningLevel)

    objects: ClassVar[LimitWarningNotificationManager] = LimitWarningNotificationManager()

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["subscription", "resource_key", "billing_period_start", "level"],
                name="uniq_limit_warning_notification",
            )
        ]

    def __str__(self):
        return (
            f"{self.subscription} - {self.resource_key} - {self.level} @ "
            f"{self.billing_period_start.isoformat()}"
        )


class BillingPeriodSummary(BaseModel):
    """One closed billing period, as a durable statement.

    ``CycleCloseService`` returns a ``ClosedPeriod`` dataclass and discards it;
    this is that value made durable. It exists because a closed period is the
    only moment at which the plan in force, the prepaid counts, the accrued
    overage, and the payment that settled it are all simultaneously knowable —
    afterwards the subscription has rolled and the prepaid counts have moved on.

    **The unique constraint is the correctness mechanism**, the same pattern
    ``MeteredOccurrence`` and ``ProviderWebhookEvent`` use: cycle close is
    idempotent on ``(subscription, period_start)`` and catch-up runs re-enter
    it, so the write must be a no-op on re-run rather than something the caller
    has to remember to check.

    No scope-filtering default manager, for the same reason ``MeteredOccurrence`` has none,
    not: billing legitimately reads across a pooled subtree, and tenant-scoped
    managers would force an ``original_manager`` escape at nearly every call
    site. ``scope`` is always the resolved **billing root**.
    """

    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="period_summaries"
    )
    scope = models.ForeignKey(
        SCOPE_MODEL,
        on_delete=models.CASCADE,
        related_name="billing_period_summaries",
    )
    billing_period_start = models.DateTimeField(db_index=True)
    billing_period_end = models.DateTimeField()

    # Plan snapshot: the plan in force for THIS period, not whatever the
    # subscription points at now. A plan change after close must not rewrite
    # history, exactly as MeteredOccurrence.unit_price is stamped at meter time.
    plan_slug = models.CharField(max_length=100)
    plan_name = models.CharField(max_length=255)
    billing_interval = models.CharField(max_length=20, choices=BillingInterval)
    currency = models.CharField(max_length=3)

    overage_total = models.DecimalField(max_digits=12, decimal_places=4)
    charged = models.BooleanField()
    payment = models.ForeignKey(
        Payment,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="period_summaries",
    )

    # Internal only — never serialized to a customer. Persisted so a disputed
    # invoice can be investigated against what reconciliation saw at the time.
    reconciliation_unmetered = models.PositiveIntegerField()
    reconciliation_orphaned = models.PositiveIntegerField()

    closed_at = models.DateTimeField()

    resources: "RelatedManager[BillingPeriodResourceUsage]"

    objects: ClassVar[BillingPeriodSummaryManager] = BillingPeriodSummaryManager()

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["subscription", "billing_period_start"],
                name="uniq_billing_period_summary",
            )
        ]
        indexes: ClassVar = [
            models.Index(
                fields=["scope", "-billing_period_start"],
                name="billing_period_org_idx",
            )
        ]
        ordering = ("-billing_period_start",)

    def __str__(self):
        return f"{self.scope_id}/{self.subscription_id} @ {self.billing_period_start.isoformat()}"


class BillingPeriodResourceUsage(BaseModel):
    """Per-resource usage as of one closed period.

    ``total`` is nullable and ``null`` means **not recorded**, never zero — the
    state of every prepaid resource for a period that closed before this feature
    shipped, and of any registered resource added after a period closed.
    Rendering "not recorded" as 0 would tell a customer they used none of
    something we simply never counted.

    ``by_scope`` maps ``scope_id -> count`` across the pooled
    subtree. A JSON blob rather than a third table because it is only ever read
    wholesale alongside its parent row; nothing filters or aggregates on it in
    SQL. Keys are written as **strings**
    (``{str(scope_id): count, ...}``), not ``int``: ``JSONField``
    serialises ``dict`` keys to strings on write regardless, so writing ``str``
    up front keeps the in-memory value ``CycleCloseService._persist_statement``
    builds identical to whatever a later read back from Postgres returns — a
    reader must ``int()`` a key before comparing it to an ``scope_id``.

    ``limit_value`` is read at **close time** for **all eight**
    registered resources, including ``event_occurrences`` — there is no
    stamped source for a period's *allowance*: ``MeteredOccurrence`` records
    ``is_within_allowance`` and ``unit_price`` per row, not the ceiling it was
    measured against, and stamping a ceiling would need a new column that is
    out of scope here. A plan or add-on change between period start and close
    is therefore reflected in every row, ``event_occurrences`` included, even
    though nothing recorded the limit mid-period.

    ``overage_unit_price`` is the one field with a stamped source, and only for
    ``event_occurrences``: when the period had at least one overage row, it is
    read back from those *stamped* ``MeteredOccurrence`` rows rather than the
    live effective limit, so a later price change cannot make this row
    disagree with the ``overage_total`` it was charged at. For the other seven
    (prepaid) resources, ``overage_unit_price`` is the as-of-close live price,
    same as ``limit_value`` — those resources have no period of their own to
    stamp against (see the "Detail = post-paid ledger only" decision above).
    See ``BillingPeriodResourceUsage.overage_unit_price`` for what a period
    with zero, or more than one, stamped price writes for
    ``event_occurrences``.
    """

    summary = models.ForeignKey(
        BillingPeriodSummary, on_delete=models.CASCADE, related_name="resources"
    )
    resource_key = models.CharField(max_length=100, choices=resource_choices)
    # null=True (not "" as DJ001 would prefer): a resource whose limit could not be
    # resolved at close time (see PlanLimit's own note that an omitted resource_key
    # otherwise reads as unlimited) must not be indistinguishable from a resource
    # explicitly classified as prepaid via an empty-string sentinel.
    kind = models.CharField(max_length=20, choices=LimitKind, null=True, blank=True)  # noqa: DJ001
    total = models.PositiveIntegerField(null=True, blank=True)
    # null == unlimited (event_occurrences) or, for the seven prepaid resources,
    # the as-of-close ceiling rather than an as-of-period one -- see the class
    # docstring.
    limit_value = models.PositiveIntegerField(null=True, blank=True)
    # null means one of: a prepaid resource, which has no overage concept; a
    # postpaid resource (event_occurrences) whose live effective limit carries no
    # price; or a postpaid resource whose period stamped more than one distinct
    # overage unit_price (a mid-period price change) and so has no single stamped
    # value that reproduces overage_total -- see the class docstring. It never
    # means "had a price but no overage this period": that case falls back to the
    # live effective limit's price instead of writing null.
    overage_unit_price = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    by_scope = models.JSONField(default=dict)

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["summary", "resource_key"],
                name="uniq_billing_period_resource_usage",
            )
        ]

    def __str__(self):
        return f"{self.summary_id} - {self.resource_key} - {self.total}"
