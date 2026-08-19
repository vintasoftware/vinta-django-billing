"""Effective limits, pooled usage counting, and entitlement lookups.

This is the engine every enforcement call site uses. Three rules matter and are
easy to break by accident:

1. **NULL is unlimited, never zero.** A ``SubscriptionPlanLimit.limit_value`` of
   ``None`` means no ceiling. So does the *absence* of a row for a resource. Both
   fail open — a missing seed row must never lock an organization out of
   something it could do yesterday.
2. **Usage pools at the billing root.** A reseller child holds no
   ``Subscription``; its usage counts against its root's ceiling together with
   every other organization in the subtree. The subtree stops at any nested
   billing root, which pays for its own subtree (see
   ``vinta_billing.services.subscription_service.is_billing_root`` — the single
   definition of that predicate, deliberately not restated here).
3. **Counting and checking must be inseparable under concurrency.**
   ``check_limit(..., lock=True)`` takes ``SELECT ... FOR UPDATE`` on the *root*
   ``Subscription`` row before counting, so two racing creates for the last unit
   of capacity serialize on one row and exactly one sees room.
"""

import logging
from collections.abc import Callable, Sequence
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Sum
from vinta_orgs.models import AbstractOrganization

from vinta_billing.constants import BillingState, LimitKind, LimitRemedy
from vinta_billing.counting import UsageContext, count_by_organization
from vinta_billing.exceptions import InapplicableUsageExtraError, OverLimitError
from vinta_billing.hierarchy import get_hierarchy, resolve_billing_root
from vinta_billing.models import (
    MeteredOccurrence,
    PaymentMethod,
    Subscription,
    SubscriptionEntitlement,
    SubscriptionPlanLimit,
)
from vinta_billing.registry import resources
from vinta_billing.services.billing_dataclasses import EffectiveLimit, LimitCheckResult
from vinta_billing.services.subscription_service import current_billing_period_start


logger = logging.getLogger(__name__)


def metered_resource_key() -> str:
    """The postpaid resource the allowance checks and the meter both work on.

    Raises rather than returning ``None``: every caller of this helper is on a
    metering path, and a metering path reached with nothing postpaid registered
    is a misconfiguration, not an empty result.
    """
    key = resources.metered_key()
    if key is None:
        raise ImproperlyConfigured(
            "No postpaid resource is registered, so there is no allowance to "
            "check. Register one with kind=LimitKind.POSTPAID -- see "
            "vinta_billing.registry."
        )
    return key


def count_metered_occurrences(context: UsageContext) -> dict[int, int]:
    """Occurrences this package's own meter recorded in the current period.

    Shipped rather than left to the project because the ledger being counted --
    ``MeteredOccurrence`` -- is this package's table. A project registering a
    postpaid resource points its counter here:

        resources.register(
            'event_occurrences',
            label=_('Event occurrences'),
            kind=LimitKind.POSTPAID,
            counter=count_metered_occurrences,
        )

    Counts the rows the meter wrote -- never a second, independent expansion of
    whatever produced them. There is deliberately only one place that decides an
    occurrence happened; a counter that re-derived it would be a second opinion,
    and the two would eventually disagree about a customer's bill.

    The period comes from ``current_billing_period_start`` rather than from
    ``Subscription.current_period_start``: the meter stamps each occurrence by
    resolving its own start time against the same function, so both sides agree
    on which period "now" falls in even when the stored column has gone stale.

    A subscription-less pool reports an empty breakdown. That under-reports, but
    this resource is postpaid, so under-reporting cannot block anybody.
    """
    subscription = context.subscription
    if subscription is None:
        return {}
    return count_by_organization(
        MeteredOccurrence.objects.for_billing_period(
            subscription.pk, current_billing_period_start(subscription)
        ).for_organizations(context.organization_ids)
    )


def _validate_usage_extra(resource_key: str, usage_extra: dict[str, Any] | None) -> None:
    """Refuse ``usage_extra`` keys ``resource_key``'s counter does not read.

    A no-op unless the resource declared ``usage_extra_keys`` at registration
    (``None``, the default, means undeclared -- see
    ``vinta_billing.registry.ResourceDefinition.usage_extra_keys``), so nothing a
    0.3.0 project already passes starts raising on upgrade.

    Also a no-op for an unregistered key: ``_usage_breakdown`` deliberately fails
    open there rather than raising mid-request, and there is no declaration to
    check against anyway.
    """
    if not usage_extra:
        return
    if resource_key not in resources:
        return
    declared = resources.get(resource_key).usage_extra_keys
    if declared is None:
        return
    unexpected = set(usage_extra) - declared
    if unexpected:
        raise InapplicableUsageExtraError(resource_key, unexpected, declared)


class EntitlementService:
    """Answers "what is the ceiling?", "how much is in use?", and "may I create one
    more?" for any organization and limited resource.

    Stateless; built by ``vinta_billing.services.container``. Read-only — nothing here
    writes, so it is safe to call from inside a caller's transaction (and
    ``check_limit(lock=True)`` requires exactly that).
    """

    def get_effective_limit(
        self, organization: AbstractOrganization, resource_key: str
    ) -> EffectiveLimit:
        """Resolve ``organization``'s ceiling for ``resource_key``.

        The value is the billing root's ``SubscriptionPlanLimit.limit_value`` plus
        the quantity of every active ``SubscriptionAddOn`` on the same resource.

        Fails open in all three "we don't know" cases — no subscription, no limit
        row for this resource, or a NULL ``limit_value`` — by returning
        ``limit_value=None`` (unlimited). Treating any of them as zero would turn a
        data gap into a total lockout, which the rollout explicitly forbids.
        """
        root = resolve_billing_root(organization)
        return self._effective_limit_for_subscription(
            self._get_subscription_for_root(root),
            resource_key,
            root.pk,
            asked_for_organization_pk=organization.pk,
        )

    def _effective_limit_for_subscription(
        self,
        subscription: Subscription | None,
        resource_key: str,
        root_pk: int | None = None,
        asked_for_organization_pk: int | None = None,
    ) -> EffectiveLimit:
        """``get_effective_limit`` given an already-resolved subscription.

        Split out so ``check_limit`` can resolve the billing root and its
        subscription **once** and reuse both, instead of re-walking the ``parent``
        chain (one query per level) and re-fetching the subscription for the
        ceiling lookup, the usage count, and the remedy.

        Resolves the ``SubscriptionPlanLimit`` row (and, when it carries a finite
        ceiling, the active add-on total) and hands both to
        ``effective_limit_from_resolved``, which is the one place the ceiling
        arithmetic itself lives. This method's own job stops at resolving those
        inputs and logging the two fail-open cases it alone can see: no
        subscription at all, and no limit row for the resource.

        :param root_pk: The **billing root**'s pk — always the root, never the
            organization that was asked about, so the warning below means one thing
            regardless of which entry point produced it. The subscription that is
            missing belongs to the root; logging a child's pk there would send
            whoever reads it looking for a subscription that was never supposed to
            exist.
        :param asked_for_organization_pk: The organization the caller actually asked
            about, when it differs from the root. Context only.
        """
        if subscription is None:
            logger.warning(
                "No subscription resolved for billing root %s (resource %s, asked for "
                "organization %s); treating the limit as unlimited. Every billing root is "
                "expected to hold exactly one Subscription — this indicates a broken "
                "invariant, not a normal state.",
                root_pk,
                resource_key,
                asked_for_organization_pk if asked_for_organization_pk is not None else root_pk,
            )
            return self.effective_limit_from_resolved(resource_key, plan_limit=None)

        limit = subscription.limits.filter(resource_key=resource_key).first()
        if limit is None:
            logger.debug(
                "Subscription %s has no SubscriptionPlanLimit row for %s; treating it as "
                "unlimited (fail-open).",
                subscription.pk,
                resource_key,
            )
            return self.effective_limit_from_resolved(resource_key, plan_limit=None)

        if limit.limit_value is None:
            # Unlimited plus any amount of purchased capacity is still unlimited;
            # skip the add-on aggregate entirely rather than adding to NULL. Passed
            # through without ever computing ``add_on_quantity`` — the delegate
            # never looks at it when ``plan_limit.limit_value is None`` either, but
            # the point is that the aggregate query itself must not run.
            return self.effective_limit_from_resolved(resource_key, plan_limit=limit)

        # NOTE: no period/expiry filter. `is_active` is the only check, so a
        # one-time (`is_recurring=False`) add-on raises the ceiling forever rather
        # than for the period it was bought for. Deactivating it is currently a
        # manual act. This belongs with the add-on purchase work that introduces
        # one-time purchases in the first place; handling expiry here would invent
        # a semantic with no spec.
        add_on_quantity = (
            subscription.add_ons.filter(resource_key=resource_key, is_active=True).aggregate(
                total=Sum("quantity")
            )["total"]
            or 0
        )
        return self.effective_limit_from_resolved(resource_key, limit, add_on_quantity)

    def effective_limit_for_subscription(
        self, subscription: Subscription | None, resource_key: str, root: AbstractOrganization
    ) -> EffectiveLimit:
        """Public entry point onto ``_effective_limit_for_subscription`` for a
        caller that already holds both ``root`` and ``subscription`` (e.g.
        ``CycleCloseService``, which resolves both once under its own
        ``SELECT ... FOR UPDATE`` and would otherwise have to import a
        module-private method to reuse them). A thin wrapper so a future change
        to the private method's signature is caught by its one call site here
        rather than silently breaking an external caller with no type or lint
        signal.
        """
        return self._effective_limit_for_subscription(
            subscription, resource_key, root_pk=root.pk, asked_for_organization_pk=root.pk
        )

    def effective_limit_from_resolved(
        self,
        resource_key: str,
        plan_limit: SubscriptionPlanLimit | None,
        add_on_quantity: int = 0,
    ) -> EffectiveLimit:
        """The one implementation of the ceiling arithmetic -- the three fail-open
        branches described below -- reached through two paths: this method, for a
        caller that has already resolved the ``SubscriptionPlanLimit`` row and the
        active add-on total for ``resource_key`` itself, and
        ``_effective_limit_for_subscription``, which resolves those same inputs
        from a ``Subscription`` and then delegates here rather than re-implementing
        the branches.

        Direct callers of this entry point resolve their own inputs to avoid
        redundant queries -- e.g. ``BillingUsageViewSet.retrieve_usage``, which
        batches ``plan_limit_by_resource``/``add_on_quantity_by_resource`` once for
        the whole resource loop specifically to avoid a
        ``SubscriptionPlanLimit`` lookup and a ``Sum`` aggregate per resource.
        Calling ``effective_limit_for_subscription`` from that loop would throw
        that batching away by re-running both queries per resource anyway.

        No row at all (``plan_limit is None`` -- also what
        ``_effective_limit_for_subscription`` passes when there is no subscription
        in the first place, or no ``SubscriptionPlanLimit`` row for the resource)
        and an explicitly unlimited row (``plan_limit.limit_value is None``) both
        resolve to ``limit_value=None`` without ever consulting ``add_on_quantity``
        for the ceiling; only a finite ``limit_value`` adds it in. Callers that
        resolve ``plan_limit`` themselves must not compute an add-on aggregate
        before knowing which branch applies -- see
        ``_effective_limit_for_subscription``'s own comment on why the aggregate
        must not run in the unlimited case.
        """
        if plan_limit is None:
            return EffectiveLimit(
                resource_key=resource_key, limit_value=None, kind=None, overage_unit_price=None
            )
        if plan_limit.limit_value is None:
            return EffectiveLimit(
                resource_key=resource_key,
                limit_value=None,
                kind=plan_limit.kind,
                overage_unit_price=plan_limit.overage_unit_price,
            )
        return EffectiveLimit(
            resource_key=resource_key,
            limit_value=plan_limit.limit_value + add_on_quantity,
            kind=plan_limit.kind,
            overage_unit_price=plan_limit.overage_unit_price,
        )

    def get_current_usage(
        self,
        organization: AbstractOrganization,
        resource_key: str,
        usage_extra: dict[str, Any] | None = None,
    ) -> int:
        """Point-in-time usage of ``resource_key``, summed across the whole pooled
        subtree that ``organization`` belongs to.

        The subtree is every organization that resolves to the same billing root:
        the root itself plus all descendants, stopping at any nested billing root
        (which pays for its own subtree separately).

        The total is not counted directly — it is ``sum(get_usage_breakdown(...))``,
        by construction (see ``_count_usage``): there is exactly one definition of
        "how much usage", and the per-organization breakdown and this scalar can
        never disagree because the scalar is derived from the breakdown, not
        computed alongside it.

        :param usage_extra: Opaque per-call data forwarded to the resource's own
            counter as ``UsageContext.extra``. The engine never reads its values,
            only checks its keys against what the resource declared -- see
            ``check_limit``.
        """
        root = resolve_billing_root(organization)
        return self._count_usage(
            root,
            resource_key,
            self._get_subscription_for_root(root),
            usage_extra=usage_extra,
        )

    def get_usage_breakdown(
        self,
        organization: AbstractOrganization,
        resource_key: str,
        usage_extra: dict[str, Any] | None = None,
    ) -> dict[int, int]:
        """Per-organization usage of ``resource_key`` across the whole pooled
        subtree that ``organization`` belongs to.

        ``get_current_usage``'s per-organization twin — same root resolution, same
        subscription lookup, same ``exclude_invitation_id`` rule. Required by the
        usage-reporting read surface (per-organization attribution across a pooled
        reseller subtree); enforcement itself only ever needs the scalar.

        An organization that contributed nothing to ``resource_key`` is **absent**
        from the returned dict, never present with ``0`` — the read layer decides
        whether a non-contributor is worth rendering.

        :param usage_extra: Opaque per-call data forwarded to the resource's own
            counter as ``UsageContext.extra``. The engine never reads its values,
            only checks its keys against what the resource declared -- see
            ``check_limit``.
        """
        root = resolve_billing_root(organization)
        return self._usage_breakdown(
            root,
            resource_key,
            self._get_subscription_for_root(root),
            usage_extra=usage_extra,
        )

    def usage_breakdown_for_root(
        self,
        root: AbstractOrganization,
        resource_key: str,
        subscription: Subscription | None,
        pooled_organization_ids: list[int] | None = None,
    ) -> dict[int, int]:
        """Public entry point onto ``_usage_breakdown`` for a caller that already
        holds ``root`` and ``subscription``. Same rationale as
        ``effective_limit_for_subscription``.

        :param pooled_organization_ids: the subtree ``root`` pools with, when the
            caller already resolved it (via ``get_pooled_organization_ids``) and
            wants to reuse it across several resources instead of paying for the
            subtree BFS again on every call -- the case ``CycleCloseService`` hits
            once per registered resource while holding the subscription
            row's lock. Resolved fresh when omitted, exactly as every other caller
            of this method already gets.
        """
        return self._usage_breakdown(
            root,
            resource_key,
            subscription,
            pooled_organization_ids=pooled_organization_ids,
        )

    def _count_usage(
        self,
        root: AbstractOrganization,
        resource_key: str,
        subscription: Subscription | None,
        usage_extra: dict[str, Any] | None = None,
    ) -> int:
        """``get_current_usage`` given an already-resolved root and subscription.

        Structurally ``sum(breakdown.values())`` — never a second, independent
        count — so this scalar and ``_usage_breakdown``'s per-organization dict
        are incapable of disagreeing about the total.
        """
        return sum(
            self._usage_breakdown(
                root, resource_key, subscription, usage_extra=usage_extra
            ).values()
        )

    def _usage_breakdown(
        self,
        root: AbstractOrganization,
        resource_key: str,
        subscription: Subscription | None,
        usage_extra: dict[str, Any] | None = None,
        pooled_organization_ids: list[int] | None = None,
    ) -> dict[int, int]:
        """``get_usage_breakdown`` given an already-resolved root and subscription.

        :param pooled_organization_ids: pre-resolved pool, when the caller already
            has it (see ``usage_breakdown_for_root``). Resolved via
            ``_get_pooled_organization_ids`` when omitted -- the behavior every
            existing caller keeps unchanged.
        :raises InapplicableUsageExtraError: if ``usage_extra`` carries a key
            ``resource_key`` declared it does not read. Checked here, at the one
            place every counter is actually invoked, so the public read methods
            (``get_current_usage``, ``get_usage_breakdown``) are covered by the
            same rule as ``check_limit`` without each restating it. ``check_limit``
            checks once more up front, because its unlimited path never reaches
            here.
        """
        _validate_usage_extra(resource_key, usage_extra)
        if resource_key not in resources:
            # Fails open rather than raising mid-request. An unregistered key
            # reaching here means a plan carries a limit row for a resource this
            # deploy does not know about -- a stale seed, or an app whose
            # registration was dropped. Reporting zero usage lets the customer
            # carry on working; raising would lock them out of an unrelated
            # feature because of a configuration gap.
            logger.warning(
                "No usage counter registered for resource %s; reporting zero usage.",
                resource_key,
            )
            return {}
        return resources.counter_for(resource_key)(
            UsageContext(
                organization_ids=(
                    pooled_organization_ids
                    if pooled_organization_ids is not None
                    else self._get_pooled_organization_ids(root)
                ),
                subscription=subscription,
                extra=usage_extra,
            )
        )

    @staticmethod
    def _lock_billing_root_row(root: AbstractOrganization) -> None:
        """Take ``SELECT ... FOR UPDATE`` on ``root``'s ``Subscription`` row.

        Discards the returned row: the point is the row lock, and every subsequent
        read in the caller's transaction goes through the same connection.
        """
        Subscription.objects.select_for_update().filter(organization=root).first()

    def lock_billing_root(self, organization: AbstractOrganization) -> None:
        """Acquire the guard lock for ``organization`` *before* computing a delta.

        ``check_limit(lock=True)`` locks and counts in one call, which is all a
        single-row create needs. A bulk writer that must first *read* the database to
        work out how many rows it is about to create (e.g. the room-import writer
        splitting discovered resources into "already counted" and "new") has to take
        the lock before that read, or it computes its delta from a snapshot a
        concurrent writer may already have invalidated.

        Re-locking the same row later in the same transaction — which
        ``check_limit(lock=True)`` will do — is a no-op, so the two compose. Held
        until the caller's transaction commits; requires an open transaction, exactly
        like ``check_limit(lock=True)``.
        """
        self._lock_billing_root_row(resolve_billing_root(organization))

    def is_billing_root_restricted(self, organization: AbstractOrganization) -> bool:
        """The single check for "must this organization's writes be blocked and
        its calendar sync paused?" -- ``True`` only when the *billing root*'s
        ``Subscription.billing_state`` is ``RESTRICTED``.

        Resolved at the billing root, like every other check in this service, so a
        reseller child answers exactly the question its root would -- the reseller
        cascade (``resolve_billing_root`` already routes children to the root) is
        automatic from that alone; nothing about the cascade needs reimplementing
        anywhere else.

        This is the **one** semantic definition of "restricted", and every
        consumer of the notion must route through it: the write block (every
        explicit ``check_not_restricted`` call site on an update/delete path) and
        whatever else a project pauses while an organization is restricted -- a
        background sync, an outbound integration, a scheduled export. Two
        *independently derived* answers to "is this org restricted" is exactly the
        recurring two-predicates defect; the definition here is the only one, and
        ``vinta_billing.signals.billing_restriction_lifted`` is how a project learns it
        has changed.

        Two hot-path guards -- ``check_limit`` and ``check_postpaid_allowance``
        below -- do **not** call this method; they inline the identical
        ``subscription.billing_state == BillingState.RESTRICTED`` test on the
        ``root`` / ``Subscription`` they have *already* resolved once, purely to
        avoid re-walking the ``parent`` chain and re-fetching the subscription on
        the two hottest create paths in the product. That is a deliberate copy of
        the same test against the same resolved state -- not an independently
        derived predicate -- so it cannot disagree with this one; each such site
        carries a comment pointing back here.

        **``GRACE`` is not restricted.** Only ``RESTRICTED`` blocks -- a ``GRACE``
        organization stays fully writable and its sync keeps running; escalation is
        the dunning ladder (``DunningService``), never a write/sync block. Do not
        widen this to any other ``BillingState``.

        A missing subscription reads as **not restricted** (``False``), never
        restricted -- ``billing_state`` only exists on a real row, and an
        organization with no billing set up at all (a broken invariant, not a
        restricted one) must not be caught by this check; that would conflate "we
        don't know" with "we know, and the answer is blocked", which the fail-open
        convention the rest of this service follows forbids.
        """
        root = resolve_billing_root(organization)
        subscription = self._get_subscription_for_root(root)
        return subscription is not None and subscription.billing_state == BillingState.RESTRICTED

    def check_not_restricted(self, organization: AbstractOrganization) -> None:
        """Raise ``OverLimitError`` (``remedy=resolve_billing``) when
        ``organization``'s billing root is ``RESTRICTED``; otherwise a no-op.

        The entry point every guarded create/update/delete method that does not
        already route through ``check_limit`` / ``check_postpaid_allowance``
        (which fold ``is_billing_root_restricted`` in directly, see their
        docstrings) calls before writing an ``OrganizationModel`` row on a guarded
        resource. See ``is_billing_root_restricted`` for what "restricted" means
        and why it is defined exactly once.
        """
        if self.is_billing_root_restricted(organization):
            raise OverLimitError.from_restricted_organization()

    def check_limit(
        self,
        organization: AbstractOrganization,
        resource_key: str,
        delta: int = 1,
        lock: bool = False,
        usage_extra: dict[str, Any] | None = None,
        usage_extra_resolver: Callable[[], dict[str, Any]] | None = None,
    ) -> LimitCheckResult:
        """Would creating ``delta`` more of ``resource_key`` stay within the ceiling?

        Resolves the billing root and its ``Subscription`` **once** and threads both
        through the ceiling lookup, the usage count, and the remedy. Doing it per
        step re-walks the ``parent`` chain (a query per level) and re-fetches the
        subscription several times on what is a guarded create path.

        On the unlimited path usage is **not counted at all** — the answer cannot
        depend on it, and every organization is on the ``unlimited`` plan for the
        whole rollout, so counting there would make every guarded create pay for a
        value nobody reads. ``LimitCheckResult.current_usage`` is ``None`` in that
        case, not ``0``: reporting a number nobody measured would be a lie a caller
        could act on.

        :param lock: When ``True``, take ``SELECT ... FOR UPDATE`` on the billing
            root's ``Subscription`` row *before* counting, so concurrent checks for
            the last unit of capacity serialize on that one row instead of both
            reading the same pre-write count and both succeeding. The lock is held
            until the caller's transaction commits, which means the caller must
            perform the actual create inside that same transaction for the
            serialization to be worth anything. Scoped to the subscription row
            rather than the resource table to keep contention off hot paths.

            Requires an open transaction. ``ATOMIC_REQUESTS = True`` satisfies this
            for anything called from a request; background jobs and management
            commands must open their own ``transaction.atomic`` block.

            Correctness depends on the connection running at **READ COMMITTED**
            (PostgreSQL's default). The second transaction
            blocks on the locked row and, on acquiring it, re-reads the resource
            tables and sees the first one's committed insert. Under REPEATABLE READ
            it would instead see its original snapshot — the same pre-write count
            the lock exists to prevent — and both callers would be allowed. If the
            project ever raises the isolation level, this guard has to be
            revisited, not just retested.
        :param usage_extra: Opaque per-call data forwarded to the resource's own
            counter as ``UsageContext.extra``. The engine never reads its
            *values*; it does check its *keys* against whatever the resource
            declared at registration, and refuses one the counter cannot read --
            see ``vinta_billing.registry.ResourceDefinition.usage_extra_keys``. A
            resource that declared nothing is unchecked, as before.
        :param usage_extra_resolver: Lazy alternative to ``usage_extra`` for a
            caller whose per-call data is itself a query -- the seat-accept case,
            where working out which invitation to exclude costs a lookup. Called
            at most once, and **only after the ceiling is known to be finite**,
            so an ``unlimited`` organization never pays for it; on the unlimited
            path usage is not counted at all, so there would be nothing to hand
            the result to. Its return value is merged into ``UsageContext.extra``
            exactly as an eager ``usage_extra`` would be, and validated the same
            way. Mutually exclusive with ``usage_extra`` -- two sources for one
            field means one of them is silently discarded, so passing both
            raises. Mirrors ``check_postpaid_allowance``'s ``delta_resolver``.
        :raises InapplicableUsageExtraError: if ``usage_extra`` (or the
            resolver's result) carries a key ``resource_key`` declared it does
            not read.
        :raises ValueError: if both ``usage_extra`` and ``usage_extra_resolver``
            are given.
        """
        if usage_extra is not None and usage_extra_resolver is not None:
            raise ValueError(
                "check_limit() takes usage_extra or usage_extra_resolver, not both -- "
                "one of the two would be silently discarded."
            )
        # Validated before the unlimited short-circuit below, not alongside the
        # count. A misrouted key on an organization with no ceiling would
        # otherwise never be reported, and "every organization is unlimited" is
        # the ordinary state of a rollout -- exactly when a call site is new and
        # most likely to be wrong.
        _validate_usage_extra(resource_key, usage_extra)

        root = resolve_billing_root(organization)
        if lock:
            self._lock_billing_root_row(root)

        subscription = self._get_subscription_for_root(root)
        # RESTRICTED blocks every write outright, independent of the
        # numeric ceiling below -- an organization whose plan carries no ceiling at
        # all (``unlimited``, every organization's actual plan for this whole
        # rollout) could otherwise create freely while RESTRICTED, since the
        # ``is_unlimited`` branch below never even looks at ``billing_state``.
        # This is the identical test ``is_billing_root_restricted`` performs,
        # inlined here against the ``root`` / ``subscription`` already resolved
        # above so this hot create path does not re-walk the ``parent`` chain and
        # re-fetch the subscription just to re-ask the same question -- see that
        # method's docstring for why the copy is deliberate and cannot diverge.
        # ``current_usage``/``ceiling`` are ``0``/``0`` sentinels here -- this
        # block is not about capacity, so there is no meaningful count to report;
        # ``remedy`` is always ``resolve_billing``, which supersedes whatever
        # ``_resolve_remedy_for`` would otherwise have picked (below, unreached
        # for a RESTRICTED subscription now that this short-circuit exists).
        if subscription is not None and subscription.billing_state == BillingState.RESTRICTED:
            return LimitCheckResult(
                allowed=False,
                resource_key=resource_key,
                current_usage=0,
                ceiling=0,
                remedy=LimitRemedy.RESOLVE_BILLING,
            )

        effective_limit = self._effective_limit_for_subscription(
            subscription, resource_key, root.pk, asked_for_organization_pk=organization.pk
        )
        if effective_limit.is_unlimited:
            return LimitCheckResult(
                allowed=True,
                resource_key=resource_key,
                current_usage=None,
                ceiling=None,
            )

        # Narrowed by the ``is_unlimited`` return above: limit_value is not None here.
        ceiling = effective_limit.limit_value or 0
        if usage_extra_resolver is not None:
            # Past the unlimited return, so the resolver's cost is only paid
            # where the count it feeds is actually read. Called exactly once.
            usage_extra = usage_extra_resolver()
            _validate_usage_extra(resource_key, usage_extra)
        current_usage = self._count_usage(root, resource_key, subscription, usage_extra=usage_extra)
        allowed = current_usage + delta <= ceiling
        return LimitCheckResult(
            allowed=allowed,
            resource_key=resource_key,
            current_usage=current_usage,
            ceiling=ceiling,
            remedy=(None if allowed else self._resolve_remedy_for(subscription, effective_limit)),
        )

    def has_payment_method(self, organization: AbstractOrganization) -> bool:
        """Does the billing root have a chargeable payment method on file, right now?

        Resolved at the billing root, like every other check in this service, so a
        reseller child asks the same question its root would answer.

        **Queries the real record** (``PaymentMethod``, ``is_active=True``). This
        used to be answered from a ``Subscription.billing_state`` allow-list proxy,
        because no payment-method record existed yet. Now
        ``SubscriptionService.record_payment_method`` writes a real record from the
        webhook path once a charge against an instrument is confirmed, and this
        method reads that record instead of inferring from billing states: once an
        instrument is actually persisted, ``billing_state`` stops being evidence of
        whether one is on file at all. An organization can be ``ACTIVE`` from a past
        cycle with no *current* instrument (e.g. after an admin removed it), or hold
        a valid card on file while ``GRACE`` — a failed charge moves
        ``ACTIVE -> GRACE`` but says nothing about whether the card itself is still
        attached, and a ``GRACE`` organization stays fully operational (only
        ``RESTRICTED`` blocks writes). Under the old proxy ``GRACE`` had to read
        ``False`` categorically, even for an organization whose card is fine and
        whose *next* retry will succeed; the real record answers that case correctly
        instead of by state-based inference.

        A missing subscription's organization has no billing root ``PaymentMethod``
        row either, so this still reads ``False`` for it — nothing to charge.
        Note that on the postpaid path this rarely decides anything — a
        subscription-less pool resolves to an unlimited ceiling and returns before
        this is ever consulted (see ``check_postpaid_allowance``).
        """
        root = resolve_billing_root(organization)
        return self._has_payment_method_for_organization_id(root.pk)

    @classmethod
    def _has_payment_method_for_subscription(cls, subscription: Subscription | None) -> bool:
        if subscription is None:
            return False
        return cls._has_payment_method_for_organization_id(subscription.organization_id)

    @staticmethod
    def _has_payment_method_for_organization_id(organization_id: int) -> bool:
        return PaymentMethod.objects.filter(
            organization_id=organization_id, is_active=True
        ).exists()

    def check_postpaid_allowance(
        self,
        organization: AbstractOrganization,
        delta: int = 1,
        lock: bool = False,
        delta_resolver: Callable[[Subscription], int] | None = None,
    ) -> LimitCheckResult:
        """Would creating ``delta`` more ``event_occurrences`` need a payment method
        this organization does not have?

        The only postpaid registered resource, so unlike ``check_limit`` this
        never takes a ``resource_key`` — there is only one to ask about.

        Unlike a prepaid ceiling, the allowance is not a hard cap. An organization
        **with** a payment method is let straight through even past it — the
        excess accrues as overage (billed at ``PlanLimit.overage_unit_price`` when
        ``MeteringService`` later meters it; this method never writes, it only
        decides whether creation may proceed). An organization **without** one is
        blocked the moment ``delta`` would take it to or past the allowance,
        because there is nothing to charge the overage to. This matches the rule:
        an organization with a payment method accrues past its included allowance
        and is never interrupted; one without a payment method is blocked at the
        allowance.

        On the unlimited path (``limit_value is None``), usage is not counted at
        all and ``current_usage``/``ceiling`` are ``None`` — identical to
        ``check_limit``'s unlimited branch, and for the same reason: every
        organization is on the ``unlimited`` plan for this whole rollout, so this
        method can never block anybody today. See the tests for that inertness
        guarantee on every guarded path.

        **Exception to all of the above: a ``RESTRICTED`` billing root
        blocks unconditionally**, before the unlimited check, before counting
        usage, and regardless of whether a payment method is on file — a
        ``RESTRICTED`` organization may not create more events even if it could
        technically pay for them; the only way out is resolving the restriction
        (``remedy=resolve_billing``), not adding a card. See
        ``is_billing_root_restricted``.

        ``delta`` must be in the same unit ``current_usage`` is measured in: the
        number of ``MeteredOccurrence`` rows this creation will eventually cause —
        **occurrences, not masters**. For a one-off event those coincide (1). For a
        *recurring* master they do not: ``MeteringService`` expands the master's rule
        and writes one row per occurrence, so a daily series costs ~30 a month, not 1.
        A caller creating a recurring master must therefore pass ``delta_resolver``
        rather than a hand-counted ``delta``.

        The other established value is the bundle fan-out's
        ``1 + n_internal_children`` (a bundle booking is billed as the primary
        calendar's event plus one more per ``CalendarProvider.INTERNAL`` child, never
        per member calendar). A caller that invents its own number here reproduces
        the "two checks that must agree" defect — derive it from the same
        provider/parent checks the meter and the fan-out writer use, never recompute
        it independently.

        :param delta_resolver: Lazy alternative to ``delta`` for a caller whose unit
            count is itself a query — specifically, expanding a just-created recurring
            master through ``MeteringService.occurrence_starts_of`` (the meter's own
            expansion, so the guard and the meter cannot disagree). Receives the
            resolved billing-root ``Subscription`` so it can bound its window with
            ``resolve_billing_period``. Called at most once, and **only after the
            ceiling is known to be finite**, so an ``unlimited`` organization — i.e.
            every organization for this whole rollout — never pays for the expansion.
            Takes precedence over ``delta`` when both are given.
        :param lock: Same contract as ``check_limit``'s ``lock`` — ``SELECT ... FOR
            UPDATE`` on the billing root's ``Subscription`` row before counting, so
            two racing creates at the allowance boundary serialize on one row.
            Requires an open transaction; see ``check_limit`` for the full isolation-
            level discussion.

            **Taken only once a finite ceiling is known to exist**, unlike
            ``check_limit``, which locks before resolving anything. That ordering
            difference is deliberate and load-bearing. Every event-creation path
            passes ``lock=True``, ``create_event`` is ``@transaction.atomic`` with an
            external provider round-trip inside it, and every organization is on
            ``unlimited`` — so locking first would put an organization-wide row lock
            on the hottest write path in the product, held across a network call, in
            service of a NULL ceiling that cannot block anybody. Two users booking
            different calendars of the same organization would serialize.

            Nothing is lost by locking later: the ceiling is not the racing quantity.
            ``_count_usage`` — the read the lock actually exists to serialize — still
            runs after the lock is acquired, and under READ COMMITTED it therefore
            still sees a racing transaction's committed inserts.
        """
        root = resolve_billing_root(organization)
        subscription = self._get_subscription_for_root(root)
        # RESTRICTED blocks outright, ahead of the unlimited check and
        # the payment-method check both. The identical test
        # ``is_billing_root_restricted`` performs, inlined here against the already
        # resolved ``root`` / ``subscription`` so this hot event-creation path does
        # not re-walk the ``parent`` chain and re-fetch the subscription to re-ask
        # the same question -- see that method's docstring. Sentinel 0/0
        # usage/ceiling, same convention as ``check_limit``'s restricted short-circuit.
        if subscription is not None and subscription.billing_state == BillingState.RESTRICTED:
            return LimitCheckResult(
                allowed=False,
                resource_key=metered_resource_key(),
                current_usage=0,
                ceiling=0,
                remedy=LimitRemedy.RESOLVE_BILLING,
            )

        effective_limit = self._effective_limit_for_subscription(
            subscription,
            metered_resource_key(),
            root.pk,
            asked_for_organization_pk=organization.pk,
        )
        if effective_limit.is_unlimited:
            return LimitCheckResult(
                allowed=True,
                resource_key=metered_resource_key(),
                current_usage=None,
                ceiling=None,
            )

        if lock:
            self._lock_billing_root_row(root)

        # Narrowed by the ``is_unlimited`` return above: limit_value is not None here.
        ceiling = effective_limit.limit_value or 0
        if delta_resolver is not None and subscription is not None:
            delta = delta_resolver(subscription)
        current_usage = self._count_usage(root, metered_resource_key(), subscription)
        within_allowance = current_usage + delta <= ceiling
        if within_allowance or self._has_payment_method_for_subscription(subscription):
            return LimitCheckResult(
                allowed=True,
                resource_key=metered_resource_key(),
                current_usage=current_usage,
                ceiling=ceiling,
            )
        # The only way to reach here is ``has_payment_method`` being False -- no
        # active ``PaymentMethod`` row on file for the billing root. The remedy is
        # always "go get a payment method", never ``_resolve_remedy_for``'s
        # billing-first branch, even for ``GRACE``: from this guard's point of view
        # there is nothing chargeable on file, and attaching a working instrument is
        # what both resolves the dunning and lifts this block. ``RESTRICTED`` never
        # reaches this branch at all -- it is short-circuited above, unconditionally,
        # before payment-method is ever consulted.
        return LimitCheckResult(
            allowed=False,
            resource_key=metered_resource_key(),
            current_usage=current_usage,
            ceiling=ceiling,
            remedy=LimitRemedy.ADD_PAYMENT_METHOD,
        )

    def has_entitlement(self, organization: AbstractOrganization, entitlement_key: str) -> bool:
        """Is the boolean feature gate ``entitlement_key`` granted to ``organization``?

        Resolved at the billing root, like limits. **Unlike limits, this fails
        closed**: an absent ``SubscriptionEntitlement`` row means "not granted",
        not "granted". The asymmetry is deliberate —
        ``SubscriptionService._sync_entitlements`` *deletes* rows for entitlements
        the current plan does not carry, so absence is how a revoked grant is
        represented. Failing open here would hand every feature to every
        organization whose plan omits it, whereas failing open on a limit only
        risks under-charging.
        """
        subscription = self._get_root_subscription(organization)
        if subscription is None:
            logger.warning(
                "No subscription resolved for organization %s; denying entitlement %s. "
                "Every billing root is expected to hold exactly one Subscription.",
                organization.pk,
                entitlement_key,
            )
            return False
        entitlement = subscription.entitlements.filter(entitlement_key=entitlement_key).first()
        return entitlement is not None and entitlement.is_enabled

    def has_entitlement_for_organizations(
        self, organizations: Sequence[AbstractOrganization], entitlement_key: str
    ) -> dict[int, bool]:
        """Bulk ``has_entitlement``: the same fail-closed boolean gate for many
        organizations, in two queries total instead of two per organization.

        Built for list endpoints that compute a per-row entitlement-derived field
        (e.g. ``MyMembershipSerializer.get_can_manage_branding`` across a
        caller's memberships) — calling ``has_entitlement`` once per row would
        pay a full subscription fetch plus entitlement-row fetch per distinct
        organization.

        Billing-root resolution stays per-organization (``resolve_billing_root``,
        unchanged): it is a ``parent``-chain walk, not something that batches
        into a single query, and it costs nothing extra for a parentless
        organization (the common case for callers that already filtered to
        roots, like ``is_branding_eligible_organization``) — only a genuinely
        nested chain triggers a query. What this method batches is the
        subscription fetch and the entitlement-row fetch, which
        ``has_entitlement`` otherwise repeats per organization.

        Returns ``{organization.pk: bool}`` for every organization passed in.
        An organization whose billing root has no resolvable subscription reads
        ``False`` (same fail-closed behavior as ``has_entitlement``), but unlike
        ``has_entitlement`` this does not log a warning per organization — doing
        so would make a list endpoint log once per row for a state that is
        normal at this call site, not the broken-invariant signal the warning
        is meant to be on the single-organization path.
        """
        roots_by_organization_pk = {
            organization.pk: resolve_billing_root(organization) for organization in organizations
        }
        root_ids = {root.pk for root in roots_by_organization_pk.values()}
        if not root_ids:
            return {}
        subscription_by_root_id = {
            subscription.organization_id: subscription
            for subscription in Subscription.objects.filter(organization_id__in=root_ids)
        }
        granted_subscription_ids = set(
            SubscriptionEntitlement.objects.filter(
                subscription_id__in=[
                    subscription.pk for subscription in subscription_by_root_id.values()
                ],
                entitlement_key=entitlement_key,
                is_enabled=True,
            ).values_list("subscription_id", flat=True)
        )
        result: dict[int, bool] = {}
        for organization_pk, root in roots_by_organization_pk.items():
            subscription = subscription_by_root_id.get(root.pk)
            result[organization_pk] = (
                subscription is not None and subscription.pk in granted_subscription_ids
            )
        return result

    def _resolve_remedy_for(
        self, subscription: Subscription | None, effective_limit: EffectiveLimit
    ) -> str:
        """Pick the ``LimitRemedy`` that will actually unblock this caller.

        An organization in grace (or, defensively, restricted) has a payment
        problem in front of any capacity problem, so it is pointed at billing
        first. Otherwise a pre-paid ceiling is liftable by buying capacity, while
        a post-paid allowance is not — only a bigger plan raises it.

        Takes the already-resolved ``subscription`` rather than re-fetching it: this
        runs on the blocked branch of ``check_limit``, which has one in hand.

        The ``RESTRICTED`` half of the ``in (...)`` below is unreachable in
        practice: ``check_limit`` short-circuits a ``RESTRICTED``
        subscription unconditionally, before this is ever called (see
        ``is_billing_root_restricted``). Left in rather than narrowed to
        ``GRACE`` alone — both source the same remedy, and removing it would make
        this function's correctness depend on exactly where its one caller happens
        to short-circuit, which is a coincidence worth not encoding twice.
        """
        if subscription is not None and subscription.billing_state in (
            BillingState.GRACE,
            BillingState.RESTRICTED,
        ):
            return LimitRemedy.RESOLVE_BILLING
        if effective_limit.kind == LimitKind.POSTPAID:
            return LimitRemedy.UPGRADE_PLAN
        return LimitRemedy.PURCHASE_ADD_ON

    def _get_root_subscription(self, organization: AbstractOrganization) -> Subscription | None:
        return self._get_subscription_for_root(resolve_billing_root(organization))

    def _get_subscription_for_root(self, root: AbstractOrganization) -> Subscription | None:
        """Fetch ``root``'s subscription without raising when it is missing.

        ``Subscription.organization`` is a ``OneToOneField``, so the reverse
        accessor raises ``RelatedObjectDoesNotExist`` rather than returning
        ``None``; every caller here wants the ``None``.
        """
        return Subscription.objects.filter(organization=root).first()

    def get_pooled_organization_ids(self, organization: AbstractOrganization) -> list[int]:
        """Every organization whose usage pools with ``organization``'s.

        Public entry point onto the same subtree walk every usage counter runs on,
        for callers that need the pool itself rather than a count —
        ``MeteringService`` sweeps calendar events across exactly this set, and it
        must be the *same* set the ``event_occurrences`` counter later reads back,
        or the meter and the counter would be looking at different organizations.
        """
        return self._get_pooled_organization_ids(resolve_billing_root(organization))

    def _get_pooled_organization_ids(self, root: AbstractOrganization) -> list[int]:
        """Every organization whose usage counts against ``root``'s ceiling.

        Delegated to the configured hierarchy strategy: whether organizations
        nest at all, and where a subtree stops, is a project's answer and not
        something this package can read off ``vinta-django-orgs``' organization
        model. See :mod:`vinta_billing.hierarchy`.

        Sorted so the pool is a stable, comparable list -- the meter and the
        counter that reads its rows back must agree on the same set, and a
        deterministic order makes a disagreement visible in a diff rather than
        intermittent.
        """
        return sorted(get_hierarchy().pooled_organization_ids(root))
