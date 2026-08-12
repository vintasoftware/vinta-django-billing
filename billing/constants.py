"""The enums that are genuinely closed.

What a payment's status can be, what states a subscription moves through, how a
limit is enforced -- these are the engine's own vocabulary and a project does not
extend them. The two sets that *are* open, the limited resources and the feature
entitlements, live in :mod:`billing.registry` instead.
"""

from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _

from billing.provider_slugs import MERCADOPAGO, STRIPE


class BillingState(TextChoices):
    """Billing lifecycle state of an organization's ``Subscription``.

    The billing state machine's transition table is the authority on the
    transitions between these states.
    """

    FREE = ("free", _("Free"))
    ACTIVE = ("active", _("Active"))
    GRACE = ("grace", _("Grace period"))
    RESTRICTED = ("restricted", _("Restricted"))
    CANCELLED = ("cancelled", _("Cancelled"))


class BillingInterval(TextChoices):
    """Billing cadence for a ``Subscription``."""

    MONTHLY = ("monthly", _("Monthly"))
    ANNUAL = ("annual", _("Annual"))


class DocumentTypes(TextChoices):
    """Kind of tax/identity document on a ``BillingProfile``.

    Sent to MercadoPago as ``payer.identification.type``; ignored by Stripe,
    which takes no document type. Not every member is accepted by every provider
    -- this enum is the set the API accepts, and each adapter's mapping is the
    per-provider translation seam. A member valid here can still be refused by a
    specific provider.
    """

    CPF = ("CPF", _("CPF"))
    CNPJ = ("CNPJ", _("CNPJ"))
    DNI = ("DNI", _("DNI"))
    CI = ("CI", _("CI"))
    RUT = ("RUT", _("RUT"))
    SSN = ("SSN", _("SSN"))
    EIN = ("EIN", _("EIN"))
    PASSPORT = ("PASSPORT", _("Passport"))
    OTHER = ("OTHER", _("Other"))


class ProviderWebhookRoute(TextChoices):
    """Which inbound webhook endpoint received a ``ProviderWebhookEvent``.

    Scopes the idempotency ledger's uniqueness alongside ``provider`` and
    ``external_event_id`` -- a provider's event-id numbering is not guaranteed to
    be disjoint between its payment and subscription-payment notification
    streams.
    """

    PAYMENT_UPDATE = ("payment_update", _("Payment update"))
    SUBSCRIPTION_PAYMENT_UPDATE = (
        "subscription_payment_update",
        _("Subscription payment update"),
    )


class LimitKind(TextChoices):
    """Whether a limited resource is capped up front or metered and billed after
    the fact."""

    PREPAID = ("prepaid", _("Prepaid"))
    POSTPAID = ("postpaid", _("Postpaid"))


class LimitRemedy(TextChoices):
    """What the caller can do about an over-limit rejection.

    Rendered verbatim as the ``remedy`` key of the shared over-limit error body
    (see ``OverLimitError``), so the client can route the user to the right
    screen instead of parsing a human-readable message.
    """

    PURCHASE_ADD_ON = ("purchase_add_on", _("Purchase additional capacity"))
    UPGRADE_PLAN = ("upgrade_plan", _("Upgrade to a plan with a higher limit"))
    ADD_PAYMENT_METHOD = ("add_payment_method", _("Add a payment method"))
    RESOLVE_BILLING = ("resolve_billing", _("Resolve an outstanding billing issue"))


class LimitWarningLevel(TextChoices):
    """How close usage is to the effective limit, as reported by
    ``UsageWarningService``.

    Two distinct notifications, each debounced independently (see
    ``LimitWarningNotification``'s unique constraint) so an organization gets
    exactly one "you're close" and, separately, exactly one "you're at your
    limit" per resource per billing cycle -- never a rising flood of duplicate
    warnings as the checker re-runs on every beat tick.
    """

    APPROACHING = ("approaching", _("Approaching the limit"))
    REACHED = ("reached", _("At or over the limit"))


class PaymentStatuses(TextChoices):
    PENDING_SEND = ("pending_send", _("Pending send"))
    PENDING = ("pending", _("Pending"))
    APPROVED = ("approved", _("Approved"))
    REJECTED = ("rejected", _("Rejected"))
    CANCELLED = ("cancelled", _("Cancelled"))
    PARTIALLY_REFUNDED = ("partially_refunded", _("Partially refunded"))
    REFUNDED = ("refunded", _("Refunded"))
    CHARGED_BACK = ("charged_back", _("Charged back"))
    IN_PROCESS = ("in_process", _("In process"))
    IN_MEDIATION = ("in_mediation", _("In mediation"))
    REJECTED_BY_BANK = ("rejected_by_bank", _("Rejected by bank"))
    EXPIRED = ("expired", _("Expired"))
    UNKNOWN = ("unknown", _("Unknown"))
    ERROR = ("error", _("Error"))


class RefundStatuses(TextChoices):
    PENDING_SEND = ("pending_send", _("Pending Send"))
    PENDING = ("pending", _("Pending"))
    APPROVED = ("approved", _("Approved"))
    REJECTED = ("rejected", _("Rejected"))
    FAILED = ("failed", _("Failed"))
    UNKNOWN = ("unknown", _("Unknown"))


class SubscriptionStatuses(TextChoices):
    ACTIVE = ("active", _("Active"))
    PAUSED = ("paused", _("Paused"))
    CANCELLED = ("cancelled", _("Cancelled"))
    PENDING = ("pending", _("Pending"))
    PENDING_SEND = ("pending_send", _("Pending send"))
    ERROR = ("error", _("Error"))
    UNKNOWN = ("unknown", _("Unknown"))


class PaymentProviders(TextChoices):
    """The providers this package ships adapters for.

    Not an open registry, unlike resources and entitlements: each member here
    corresponds to an adapter implementation in ``billing.services``. A project
    adding its own provider registers the adapter and stores its slug in the
    same column -- the field's ``max_length`` accommodates that -- but does not
    extend this enum.
    """

    MERCADOPAGO = (MERCADOPAGO, "MercadoPago")
    STRIPE = (STRIPE, "Stripe")
