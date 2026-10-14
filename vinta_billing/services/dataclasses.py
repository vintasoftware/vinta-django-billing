import datetime
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class BillingAddress:
    id: int | None
    street_name: str
    street_number: str
    neighborhood: str | None
    address_line_2: str | None
    city: str
    state: str
    country: str
    zip_code: str

    @property
    def address_line_1(self):
        return f"{self.street_name} {self.street_number}"

    def __str__(self):
        return (
            f"{self.id} {self.address_line_1}, {self.address_line_2}"
            f"{f', {self.neighborhood}' if self.neighborhood else ''} - {self.city} "
            f"- {self.state} - {self.country} - {self.zip_code}"
        )


@dataclass
class BillingProfile:
    pk: int | None
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    document_type: str | None
    document_number: str | None
    billing_address: BillingAddress

    def __str__(self):
        return f"{self.first_name} {self.last_name} - {self.email} - {self.phone}"


@dataclass
class Plan:
    id: int
    name: str
    value: Decimal
    currency: str
    billing_day: int
    # Required, not defaulted: MercadoPago's `create_subscription_plan` used to
    # hardcode a monthly cadence, which silently made annual plans impossible.
    # Stripe's `Price` objects require an explicit `recurring.interval` at
    # creation time, which surfaced the gap — both adapters now read this instead
    # of assuming monthly. One of `vinta_billing.constants.BillingInterval`.
    billing_interval: str


@dataclass
class CreatedPlan(Plan):
    external_id: str


@dataclass
class Subscription:
    id: int
    status: str
    external_id: str | None
    billing_profile: BillingProfile
    plan: CreatedPlan
    start_date: str
    end_date: str


@dataclass
class PaymentStatusUpdate:
    id: int | None  # It can be None because it may not be persisted yet
    status: str
    description: str | None
    update_external_id: str | None
    # The provider's own, unmapped status (MercadoPago's ``"authorized"``,
    # Stripe's ``"succeeded"``), persisted as ``Payment.original_status``
    # alongside the mapped ``status``. Defaulted so an adapter written before it
    # existed still constructs this the same way; ``None`` leaves the column as
    # it was.
    original_status: str | None = None

    def __str__(self):
        return f"{self.id} {self.status} - {self.description}"


@dataclass
class Payment:
    id: int | None
    value: Decimal
    currency: str
    payment_provider: str
    external_id: str
    status: str
    billing_profile: BillingProfile
    payment_method: str
    description: str
    status_updates: list[PaymentStatusUpdate]


@dataclass
class Refund:
    id: int
    payment: Payment
    value: Decimal
    currency: str
    # `None` when constructing a `Refund` to *request* a new refund (the provider
    # is what assigns this). Required when constructing one to *poll* an existing
    # refund's status via `check_refund_status` — MercadoPago has no
    # single-refund-by-id lookup, only a list-by-payment one, so the id (and the
    # parent payment's own external id, via `payment.external_id`) must both be
    # available at that point.
    external_id: str | None = None
    # Caller-supplied, stable across retries: forwarded as the provider's own
    # idempotency key so a retried refund resolves to the refund the first
    # attempt created. Empty means the caller supplied none; each adapter
    # documents its own fallback.
    idempotency_key: str = ""

    def __str__(self):
        return f"{self.id} {self.payment} - {self.value} - {self.currency}"


@dataclass
class RefundResult:
    """The immediate result of requesting a refund from the provider.

    Both MercadoPago and Stripe return the refund's status synchronously in the
    same response body that carries its id — there is no reason to force a
    second round trip (``check_refund_status``) just to learn a status the
    create-refund response already carried. ``check_refund_status`` still exists
    for later reconciliation (an async pending -> succeeded/failed transition, or
    a scheduled cycle-close sweep); it is simply no longer the *only* way to
    learn a refund's status.
    """

    external_id: str
    status: str


@dataclass
class ChargeResult:
    """What the provider said when a charge was created.

    Returned by ``BasePaymentAdapter.process_charge``. Only ``external_id`` is
    required: the base implementation wraps an adapter's plain ``process()`` and
    knows nothing else, so every other field is optional and ``None`` means "the
    provider did not say".
    """

    external_id: str
    #: Mapped to ``PaymentStatuses``. Written to ``Payment.status`` through the
    #: same rules as a webhook update, so a charge approved synchronously is not
    #: reported a second time when the webhook confirms it.
    status: str | None = None
    #: The provider's own, unmapped status.
    original_status: str | None = None
    #: Provider-specific data a caller needs to complete the charge -- for a Pix
    #: charge, the QR code and the ticket URL. Persisted as
    #: ``Payment.provider_data``.
    provider_data: dict | None = None
    #: When the provider will stop accepting payment for this charge.
    expires_at: datetime.datetime | None = None


@dataclass
class PixCharge:
    """What a payer needs to pay a Pix charge, as returned by
    ``PaymentService.get_pix_charge``."""

    payment_id: int
    external_id: str
    status: str
    qr_code: str
    qr_code_base64: str
    ticket_url: str
    expires_at: datetime.datetime | None


@dataclass
class SubscriptionPayment(Payment):
    subscription_external_id: str
