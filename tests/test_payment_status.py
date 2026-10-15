"""``Payment.status`` and ``Refund.status`` follow the provider, and say so once.

Covers the rules in ``vinta_billing.services.payment_status_machine`` and every
place ``PaymentService`` writes a status through them: the charge response
(``create_payment``), the payment and subscription-payment webhooks, the refund
response (``create_refund``) and the refund poll (``check_refund_status``). The
end-to-end webhook delivery, through the view and a signed request, is in
``tests/test_payment_webhook_flow.py``.
"""

import datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest
from django.db import transaction
from django.utils import timezone

from vinta_billing.constants import PaymentProviders, PaymentStatuses, RefundStatuses
from vinta_billing.models import Payment, PaymentStatusUpdate, Refund
from vinta_billing.services.dataclasses import (
    ChargeResult,
    PixCharge,
    RefundResult,
    SubscriptionPayment,
)
from vinta_billing.services.dataclasses import (
    PaymentStatusUpdate as PaymentStatusUpdateData,
)
from vinta_billing.services.payment_service import PaymentService
from vinta_billing.services.payment_status_machine import (
    FINAL_PAYMENT_STATUSES,
    can_move_payment_status,
    can_move_refund_status,
    record_payment_status,
)
from vinta_billing.signals import payment_status_changed, refund_status_changed


MP = PaymentProviders.MERCADOPAGO


class FakePaymentAdapter:
    """Just enough of a payment adapter for ``PaymentService``: answers are set
    per test, and every call is recorded."""

    provider = MP
    is_configured = True

    def __init__(self):
        self.charge = ChargeResult(external_id="ext-1")
        self.refund_result = RefundResult(external_id="r-ext-1", status=RefundStatuses.APPROVED)
        self.refund_status = RefundStatuses.APPROVED
        self.update: tuple[str, PaymentStatusUpdateData] | None = None
        self.charges: list[dict] = []
        self.refunds: list = []

    def process_charge(self, payment, payment_token, idempotency_key="", *, expires_at=None):
        self.charges.append(
            {
                "payment_token": payment_token,
                "idempotency_key": idempotency_key,
                "expires_at": expires_at,
            }
        )
        return self.charge

    def refund(self, refund):
        self.refunds.append(refund)
        return self.refund_result

    def check_refund_status(self, refund):
        return self.refund_status

    def receive_update(self, payload):
        return self.update


class FakeSubscriptionAdapter:
    provider = MP
    is_configured = True

    def __init__(self):
        self.update = None

    def receive_payment_update(self, payload):
        return self.update


@pytest.fixture
def adapter():
    return FakePaymentAdapter()


@pytest.fixture
def subscription_adapter():
    return FakeSubscriptionAdapter()


@pytest.fixture
def service(adapter, subscription_adapter):
    resolver = Mock()
    resolver.resolve_for_scope.return_value = MP
    return PaymentService(
        payment_provider_resolver=resolver,
        payment_provider_registry={MP: adapter},
        subscription_provider_registry={MP: subscription_adapter},
    )


@pytest.fixture
def payment(billing_profile):
    return Payment.objects.create(
        billing_profile=billing_profile,
        currency="BRL",
        value=Decimal("10.00"),
        description="Synthetic charge",
        payment_method="visa",
        status=PaymentStatuses.PENDING,
        payment_provider=MP,
        external_id="ext-1",
    )


@pytest.fixture
def payment_signals():
    received: list[dict] = []

    def receiver(sender, **kwargs):
        received.append(kwargs)

    payment_status_changed.connect(receiver)
    yield received
    payment_status_changed.disconnect(receiver)


@pytest.fixture
def refund_signals():
    received: list[dict] = []

    def receiver(sender, **kwargs):
        received.append(kwargs)

    refund_status_changed.connect(receiver)
    yield received
    refund_status_changed.disconnect(receiver)


def update(status, original_status=None):
    return PaymentStatusUpdateData(
        id=None,
        status=status,
        description="synthetic",
        update_external_id="ext-1",
        original_status=original_status,
    )


# --- The rules ---


class TestPaymentStatusRules:
    @pytest.mark.parametrize("final", sorted(FINAL_PAYMENT_STATUSES))
    @pytest.mark.parametrize("stale", [PaymentStatuses.PENDING, PaymentStatuses.APPROVED])
    def test_a_final_status_never_moves(self, final, stale):
        assert can_move_payment_status(final, stale) is False

    @pytest.mark.parametrize(
        "stale", [PaymentStatuses.PENDING, PaymentStatuses.IN_PROCESS, PaymentStatuses.UNKNOWN]
    )
    def test_an_approved_charge_does_not_go_back_to_open(self, stale):
        assert can_move_payment_status(PaymentStatuses.APPROVED, stale) is False

    @pytest.mark.parametrize(
        "to_status",
        [
            PaymentStatuses.REFUNDED,
            PaymentStatuses.PARTIALLY_REFUNDED,
            PaymentStatuses.CHARGED_BACK,
            PaymentStatuses.IN_MEDIATION,
        ],
    )
    def test_an_approved_charge_moves_forward(self, to_status):
        assert can_move_payment_status(PaymentStatuses.APPROVED, to_status) is True

    def test_a_dispute_can_be_decided_for_the_seller(self):
        assert can_move_payment_status(PaymentStatuses.IN_MEDIATION, PaymentStatuses.APPROVED)

    @pytest.mark.parametrize(
        "to_status",
        [
            PaymentStatuses.APPROVED,
            PaymentStatuses.REJECTED,
            PaymentStatuses.CANCELLED,
            PaymentStatuses.EXPIRED,
            PaymentStatuses.IN_PROCESS,
        ],
    )
    def test_an_open_charge_moves_anywhere(self, to_status):
        assert can_move_payment_status(PaymentStatuses.PENDING, to_status) is True

    def test_unknown_does_not_overwrite_a_reported_status(self):
        assert can_move_payment_status(PaymentStatuses.PENDING, PaymentStatuses.UNKNOWN) is False
        assert can_move_payment_status(PaymentStatuses.PENDING_SEND, PaymentStatuses.UNKNOWN)

    def test_nothing_returns_to_pending_send(self):
        assert (
            can_move_payment_status(PaymentStatuses.PENDING, PaymentStatuses.PENDING_SEND) is False
        )

    def test_the_same_status_is_not_a_move(self):
        assert can_move_payment_status(PaymentStatuses.PENDING, PaymentStatuses.PENDING) is False


class TestRefundStatusRules:
    @pytest.mark.parametrize("final", [RefundStatuses.APPROVED, RefundStatuses.REJECTED])
    def test_a_final_refund_never_moves(self, final):
        assert can_move_refund_status(final, RefundStatuses.PENDING) is False

    def test_unknown_does_not_overwrite_a_reported_status(self):
        assert can_move_refund_status(RefundStatuses.PENDING, RefundStatuses.UNKNOWN) is False

    def test_a_pending_refund_settles(self):
        assert can_move_refund_status(RefundStatuses.PENDING, RefundStatuses.APPROVED) is True


# --- record_payment_status ---


@pytest.mark.django_db
class TestRecordPaymentStatus:
    def test_writes_status_and_original_status(self, payment, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            changed = record_payment_status(payment, PaymentStatuses.APPROVED, "accredited")

        assert changed is True
        payment.refresh_from_db()
        assert payment.status == PaymentStatuses.APPROVED
        assert payment.original_status == "accredited"

    def test_sends_once_on_commit_not_before(
        self, payment, payment_signals, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            record_payment_status(payment, PaymentStatuses.APPROVED, "approved")
        assert payment_signals == []

        for callback in callbacks:
            callback()
        assert payment_signals == [
            {
                "signal": payment_status_changed,
                "payment": payment,
                "from_status": PaymentStatuses.PENDING,
                "to_status": PaymentStatuses.APPROVED,
            }
        ]

    def test_the_same_status_again_sends_nothing(
        self, payment, payment_signals, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            record_payment_status(payment, PaymentStatuses.APPROVED, "approved")
            changed = record_payment_status(payment, PaymentStatuses.APPROVED, "approved")

        assert changed is False
        assert len(payment_signals) == 1

    def test_a_stale_update_does_not_move_a_terminal_status(
        self, payment, payment_signals, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            record_payment_status(payment, PaymentStatuses.APPROVED, "approved")
            changed = record_payment_status(payment, PaymentStatuses.PENDING, "pending")

        assert changed is False
        payment.refresh_from_db()
        assert payment.status == PaymentStatuses.APPROVED
        assert payment.original_status == "approved"
        assert [s["to_status"] for s in payment_signals] == [PaymentStatuses.APPROVED]

    def test_a_rolled_back_change_sends_nothing(
        self, payment, payment_signals, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            with pytest.raises(RuntimeError):
                with transaction.atomic():
                    record_payment_status(payment, PaymentStatuses.APPROVED, "approved")
                    raise RuntimeError("caller failed after the write")

        assert payment_signals == []
        payment.refresh_from_db()
        assert payment.status == PaymentStatuses.PENDING

    def test_reads_the_stored_status_not_a_stale_instance(
        self, payment, payment_signals, django_capture_on_commit_callbacks
    ):
        """Two copies of one row: the second must see what the first wrote."""
        other_copy = Payment.objects.get(pk=payment.pk)
        with django_capture_on_commit_callbacks(execute=True):
            record_payment_status(payment, PaymentStatuses.APPROVED, "approved")
            changed = record_payment_status(other_copy, PaymentStatuses.APPROVED, "approved")

        assert changed is False
        assert other_copy.status == PaymentStatuses.APPROVED
        assert len(payment_signals) == 1


# --- create_payment ---


@pytest.mark.django_db
class TestCreatePayment:
    def test_writes_the_status_the_charge_answered_with(
        self,
        service,
        adapter,
        scope,
        billing_profile,
        payment_signals,
        django_capture_on_commit_callbacks,
    ):
        adapter.charge = ChargeResult(
            external_id="ext-9", status=PaymentStatuses.APPROVED, original_status="approved"
        )
        with django_capture_on_commit_callbacks(execute=True):
            payment = service.create_payment(
                scope, "BRL", Decimal("10.00"), "Synthetic", "visa", "card-token"
            )

        payment.refresh_from_db()
        assert payment.external_id == "ext-9"
        assert payment.status == PaymentStatuses.APPROVED
        assert payment.original_status == "approved"
        assert [(s["from_status"], s["to_status"]) for s in payment_signals] == [
            (PaymentStatuses.PENDING_SEND, PaymentStatuses.APPROVED)
        ]

    def test_an_adapter_reporting_only_an_id_leaves_the_status(
        self,
        service,
        adapter,
        scope,
        billing_profile,
        payment_signals,
        django_capture_on_commit_callbacks,
    ):
        """The base ``process_charge`` wraps ``process`` and knows only the id."""
        with django_capture_on_commit_callbacks(execute=True):
            payment = service.create_payment(
                scope, "BRL", Decimal("10.00"), "Synthetic", "visa", "card-token"
            )

        payment.refresh_from_db()
        assert payment.status == PaymentStatuses.PENDING_SEND
        assert payment_signals == []

    def test_the_old_positional_call_still_works(self, service, adapter, scope, billing_profile):
        service.create_payment(
            scope, "BRL", Decimal("10.00"), "Synthetic", "visa", "card-token", "idem-1"
        )

        assert adapter.charges == [
            {"payment_token": "card-token", "idempotency_key": "idem-1", "expires_at": None}
        ]

    def test_stores_a_pix_charge_and_exposes_it(
        self, service, adapter, scope, billing_profile, django_capture_on_commit_callbacks
    ):
        expires_at = timezone.now() + datetime.timedelta(minutes=30)
        adapter.charge = ChargeResult(
            external_id="pix-1",
            status=PaymentStatuses.PENDING,
            original_status="pending",
            provider_data={
                "qr_code": "synthetic-copy-and-paste",
                "qr_code_base64": "c3ludGhldGlj",
                "ticket_url": "https://example.com/ticket",
            },
            expires_at=expires_at,
        )
        with django_capture_on_commit_callbacks(execute=True):
            payment = service.create_payment(
                scope, "BRL", Decimal("10.00"), "Synthetic", "pix", expires_at=expires_at
            )

        assert adapter.charges[0]["payment_token"] == ""
        assert adapter.charges[0]["expires_at"] == expires_at
        stored = Payment.objects.get(pk=payment.pk)
        assert service.get_pix_charge(stored) == PixCharge(
            payment_id=payment.pk,
            external_id="pix-1",
            status=PaymentStatuses.PENDING,
            qr_code="synthetic-copy-and-paste",
            qr_code_base64="c3ludGhldGlj",
            ticket_url="https://example.com/ticket",
            expires_at=expires_at,
        )

    def test_a_card_charge_has_no_pix_charge(self, service, payment):
        assert service.get_pix_charge(payment) is None


# --- Webhook paths ---


@pytest.mark.django_db
class TestReceivePaymentUpdate:
    def test_writes_the_update_row_and_the_status(
        self, service, adapter, payment, payment_signals, django_capture_on_commit_callbacks
    ):
        adapter.update = ("ext-1", update(PaymentStatuses.APPROVED, "approved"))

        with django_capture_on_commit_callbacks(execute=True):
            status_update = service.receive_payment_update({}, provider=MP)

        assert status_update.status == PaymentStatuses.APPROVED
        payment.refresh_from_db()
        assert payment.status == PaymentStatuses.APPROVED
        assert payment.original_status == "approved"
        assert len(payment_signals) == 1

    def test_a_stale_update_is_history_not_a_change(
        self, service, adapter, payment, payment_signals, django_capture_on_commit_callbacks
    ):
        payment.status = PaymentStatuses.APPROVED
        payment.save()
        adapter.update = ("ext-1", update(PaymentStatuses.PENDING, "pending"))

        with django_capture_on_commit_callbacks(execute=True):
            service.receive_payment_update({}, provider=MP)

        assert PaymentStatusUpdate.objects.filter(payment=payment).count() == 1
        payment.refresh_from_db()
        assert payment.status == PaymentStatuses.APPROVED
        assert payment_signals == []


@pytest.mark.django_db
class TestReceiveSubscriptionPaymentUpdate:
    def _subscription_update(self, subscription, status, original_status):
        payment_data = SubscriptionPayment(
            id=None,
            value=Decimal("10.00"),
            currency="BRL",
            payment_provider=MP,
            external_id="sub-charge-1",
            status=original_status,
            billing_profile=Mock(),
            payment_method="visa",
            description="Synthetic renewal",
            status_updates=[],
            subscription_external_id=subscription.external_id,
        )
        return payment_data, update(status, original_status)

    def test_a_new_charge_is_recorded_and_announced(
        self,
        service,
        subscription_adapter,
        subscription,
        billing_profile,
        payment_signals,
        django_capture_on_commit_callbacks,
    ):
        subscription.external_id = "sub-ext-1"
        subscription.save()
        subscription_adapter.update = self._subscription_update(
            subscription, PaymentStatuses.APPROVED, "authorized"
        )

        with django_capture_on_commit_callbacks(execute=True):
            service.receive_subscription_payment_update({}, provider=MP)

        payment = Payment.objects.get(external_id="sub-charge-1")
        assert payment.status == PaymentStatuses.APPROVED
        assert payment.original_status == "authorized"
        assert [(s["from_status"], s["to_status"]) for s in payment_signals] == [
            (PaymentStatuses.PENDING, PaymentStatuses.APPROVED)
        ]

    def test_an_existing_charge_moves(
        self,
        service,
        subscription_adapter,
        subscription,
        billing_profile,
        payment_signals,
        django_capture_on_commit_callbacks,
    ):
        subscription.external_id = "sub-ext-1"
        subscription.save()
        subscription_adapter.update = self._subscription_update(
            subscription, PaymentStatuses.PENDING, "pending"
        )
        with django_capture_on_commit_callbacks(execute=True):
            service.receive_subscription_payment_update({}, provider=MP)
        subscription_adapter.update = self._subscription_update(
            subscription, PaymentStatuses.REJECTED, "rejected"
        )
        with django_capture_on_commit_callbacks(execute=True):
            service.receive_subscription_payment_update({}, provider=MP)

        payment = Payment.objects.get(external_id="sub-charge-1")
        assert payment.status == PaymentStatuses.REJECTED
        assert [s["to_status"] for s in payment_signals] == [PaymentStatuses.REJECTED]


# --- Refunds ---


@pytest.mark.django_db
class TestCreateRefund:
    def test_threads_the_idempotency_key_to_the_adapter(self, service, adapter, payment):
        service.create_refund(payment.pk, Decimal("5.00"), "BRL", idempotency_key="refund-key-1")

        assert adapter.refunds[0].idempotency_key == "refund-key-1"

    def test_the_same_key_returns_the_same_refund_without_a_second_call(
        self, service, adapter, payment
    ):
        first = service.create_refund(
            payment.pk, Decimal("5.00"), "BRL", idempotency_key="refund-key-1"
        )
        second = service.create_refund(
            payment.pk, Decimal("5.00"), "BRL", idempotency_key="refund-key-1"
        )

        assert first.pk == second.pk
        assert len(adapter.refunds) == 1
        assert Refund.objects.count() == 1

    def test_a_key_reused_for_another_payment_is_refused(
        self, service, adapter, payment, billing_profile
    ):
        service.create_refund(payment.pk, Decimal("5.00"), "BRL", idempotency_key="refund-key-1")
        other = Payment.objects.create(
            billing_profile=billing_profile,
            currency="BRL",
            value=Decimal("10.00"),
            payment_method="visa",
            status=PaymentStatuses.APPROVED,
            payment_provider=MP,
            external_id="ext-2",
        )

        with pytest.raises(ValueError, match="different payment"):
            service.create_refund(other.pk, Decimal("5.00"), "BRL", idempotency_key="refund-key-1")

    def test_without_a_key_every_call_refunds(self, service, adapter, payment):
        service.create_refund(payment.pk, Decimal("5.00"), "BRL")
        service.create_refund(payment.pk, Decimal("5.00"), "BRL")

        assert len(adapter.refunds) == 2

    def test_announces_the_providers_answer_once(
        self, service, adapter, payment, refund_signals, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            refund = service.create_refund(payment.pk, Decimal("5.00"), "BRL")

        refund.refresh_from_db()
        assert refund.status == RefundStatuses.APPROVED
        assert refund.external_id == "r-ext-1"
        assert refund_signals == [
            {
                "signal": refund_status_changed,
                "refund": refund,
                "from_status": RefundStatuses.PENDING_SEND,
                "to_status": RefundStatuses.APPROVED,
            }
        ]

    def test_a_failed_provider_call_is_announced_as_failed(
        self, service, adapter, payment, refund_signals, django_capture_on_commit_callbacks
    ):
        adapter.refund = Mock(side_effect=RuntimeError("provider down"))

        with django_capture_on_commit_callbacks(execute=True):
            refund = service.create_refund(payment.pk, Decimal("5.00"), "BRL")

        refund.refresh_from_db()
        assert refund.status == RefundStatuses.FAILED
        assert [s["to_status"] for s in refund_signals] == [RefundStatuses.FAILED]


@pytest.mark.django_db
class TestCheckRefundStatus:
    @pytest.fixture
    def refund(self, payment):
        return Refund.objects.create(
            payment=payment,
            value=Decimal("5.00"),
            currency="BRL",
            external_id="r-ext-1",
            status=RefundStatuses.PENDING,
        )

    def test_a_settled_refund_is_announced(
        self, service, adapter, refund, refund_signals, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            service.check_refund_status(refund)
            service.check_refund_status(refund)

        refund.refresh_from_db()
        assert refund.status == RefundStatuses.APPROVED
        assert len(refund_signals) == 1

    def test_a_poll_that_cannot_find_the_refund_changes_nothing(
        self, service, adapter, refund, refund_signals, django_capture_on_commit_callbacks
    ):
        adapter.refund_status = RefundStatuses.UNKNOWN

        with django_capture_on_commit_callbacks(execute=True):
            service.check_refund_status(refund)

        refund.refresh_from_db()
        assert refund.status == RefundStatuses.PENDING
        assert refund_signals == []


@pytest.mark.django_db
def test_a_duck_typed_adapter_without_process_charge_still_charges(scope, billing_profile):
    """A project adapter that never subclassed ``BasePaymentAdapter`` has only
    ``process``; ``create_payment`` falls back to it and records the id alone."""

    class LegacyAdapter:
        is_configured = True

        def process(self, payment, payment_token, idempotency_key=""):
            return "legacy-ext-1"

    resolver = Mock()
    resolver.resolve_for_scope.return_value = MP
    service = PaymentService(
        payment_provider_resolver=resolver,
        payment_provider_registry={MP: LegacyAdapter()},
        subscription_provider_registry={},
    )

    payment = service.create_payment(scope, "BRL", Decimal("10.00"), "Synthetic", "visa", "tok")

    payment.refresh_from_db()
    assert payment.external_id == "legacy-ext-1"
    assert payment.status == PaymentStatuses.PENDING_SEND
