"""Reading a payment's status from the provider when no webhook comes.

``PaymentService.sync_payment_status`` / ``sync_payment_status_on_read``, the
``jobs.sync_open_payments`` sweep, and the ``PAYMENT_STATUS_SYNC`` settings. The
adapter is faked at its boundary (``check_status``); everything from there on --
the history row, the status rules, the signal -- is the real code.
"""

import datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time

from vinta_billing import jobs
from vinta_billing.conf import get_payment_status_sync_setting
from vinta_billing.constants import PaymentProviders, PaymentStatuses, PaymentStatusUpdateSource
from vinta_billing.exceptions import ProviderRequestRejectedError
from vinta_billing.models import Payment, PaymentStatusUpdate
from vinta_billing.services.dataclasses import PaymentStatusUpdate as PaymentStatusUpdateData
from vinta_billing.services.payment_service import PaymentService
from vinta_billing.signals import payment_status_changed


MP = PaymentProviders.MERCADOPAGO


class FakeAdapter:
    provider = MP
    is_configured = True

    def __init__(self):
        self.answer: PaymentStatusUpdateData | Exception = reported(PaymentStatuses.APPROVED)
        self.asked: list[str] = []

    def check_status(self, payment_external_id, update_id=None):
        self.asked.append(payment_external_id)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def reported(status, original_status="processed"):
    return PaymentStatusUpdateData(
        id=None,
        status=status,
        description="accredited",
        update_external_id="ORD01SYNC",
        original_status=original_status,
    )


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def adapter():
    return FakeAdapter()


@pytest.fixture
def service(adapter):
    resolver = Mock()
    resolver.resolve_for_scope.return_value = MP
    return PaymentService(
        payment_provider_resolver=resolver,
        payment_provider_registry={MP: adapter},
        subscription_provider_registry={},
    )


@pytest.fixture
def make_payment(billing_profile):
    def make(status=PaymentStatuses.PENDING, external_id="ORD01SYNC", age=None):
        payment = Payment.objects.create(
            billing_profile=billing_profile,
            currency="BRL",
            value=Decimal("150.00"),
            description="Course",
            payment_method="pix",
            status=status,
            payment_provider=MP,
            external_id=external_id,
        )
        if age is not None:
            Payment.objects.filter(pk=payment.pk).update(created=timezone.now() - age)
        return payment

    return make


@pytest.fixture
def signals():
    received: list[tuple[str, str]] = []

    def receiver(sender, payment, from_status, to_status, **kwargs):
        received.append((from_status, to_status))

    payment_status_changed.connect(receiver)
    yield received
    payment_status_changed.disconnect(receiver)


@pytest.mark.django_db
class TestSyncPaymentStatus:
    def test_an_approved_answer_is_recorded_like_a_webhook(
        self, service, adapter, make_payment, signals, django_capture_on_commit_callbacks
    ):
        payment = make_payment()

        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            changed = service.sync_payment_status(payment)
            assert signals == []

        assert changed is True
        assert len(callbacks) == 1
        assert signals == [(PaymentStatuses.PENDING, PaymentStatuses.APPROVED)]
        payment.refresh_from_db()
        assert payment.status == PaymentStatuses.APPROVED
        assert payment.original_status == "processed"
        (row,) = PaymentStatusUpdate.objects.filter(payment=payment)
        assert row.status == PaymentStatuses.APPROVED
        assert row.source == PaymentStatusUpdateSource.SYNC
        assert row.external_id == "ORD01SYNC"
        assert adapter.asked == ["ORD01SYNC"]

    def test_syncing_again_changes_and_sends_nothing(
        self, service, adapter, make_payment, signals, django_capture_on_commit_callbacks
    ):
        payment = make_payment()
        with django_capture_on_commit_callbacks(execute=True):
            service.sync_payment_status(payment)

        with django_capture_on_commit_callbacks(execute=True):
            assert service.sync_payment_status(payment) is False

        assert len(signals) == 1
        assert PaymentStatusUpdate.objects.filter(payment=payment).count() == 1

    def test_the_status_already_stored_writes_nothing(
        self, service, adapter, make_payment, signals, django_capture_on_commit_callbacks
    ):
        """A sweep asks about a pending Pix charge many times before it is paid."""
        payment = make_payment()
        adapter.answer = reported(PaymentStatuses.PENDING, "action_required")

        with django_capture_on_commit_callbacks(execute=True):
            assert service.sync_payment_status(payment) is False

        assert adapter.asked == ["ORD01SYNC"]
        assert signals == []
        assert not PaymentStatusUpdate.objects.filter(payment=payment).exists()

    def test_in_process_is_open_too(self, service, adapter, make_payment):
        payment = make_payment(status=PaymentStatuses.IN_PROCESS)

        assert service.sync_payment_status(payment) is True

    @pytest.mark.parametrize(
        ("status", "external_id"),
        [
            (PaymentStatuses.APPROVED, "ORD01SYNC"),
            (PaymentStatuses.REJECTED, "ORD01SYNC"),
            (PaymentStatuses.REFUNDED, "ORD01SYNC"),
            (PaymentStatuses.PENDING_SEND, ""),
            (PaymentStatuses.PENDING, ""),
        ],
    )
    def test_a_payment_that_is_not_open_is_not_sent_to_the_provider(
        self, service, adapter, make_payment, status, external_id
    ):
        payment = make_payment(status=status, external_id=external_id)

        assert service.sync_payment_status(payment) is False
        assert adapter.asked == []

    def test_a_refused_request_leaves_the_payment_as_it_was(
        self, service, adapter, make_payment, caplog
    ):
        payment = make_payment()
        adapter.answer = ProviderRequestRejectedError(
            MP, 404, "Order not found", (("order_not_found", "Order not found"),)
        )

        assert service.sync_payment_status(payment) is False

        payment.refresh_from_db()
        assert payment.status == PaymentStatuses.PENDING
        assert not PaymentStatusUpdate.objects.filter(payment=payment).exists()
        assert "ProviderRequestRejectedError" in caplog.text
        assert payment.billing_profile.contact_email not in caplog.text

    def test_an_unknown_status_leaves_the_payment_as_it_was(self, service, adapter, make_payment):
        payment = make_payment()
        adapter.answer = reported(PaymentStatuses.UNKNOWN, "something_new")

        assert service.sync_payment_status(payment) is False

        payment.refresh_from_db()
        assert payment.status == PaymentStatuses.PENDING
        assert not PaymentStatusUpdate.objects.filter(payment=payment).exists()

    def test_min_interval_limits_how_often_the_provider_is_asked(
        self, service, adapter, make_payment
    ):
        payment = make_payment()
        adapter.answer = reported(PaymentStatuses.PENDING, "action_required")
        interval = datetime.timedelta(seconds=10)

        with freeze_time("2026-10-22 12:00:00") as frozen:
            service.sync_payment_status(payment, min_interval=interval)
            frozen.tick(datetime.timedelta(seconds=5))
            service.sync_payment_status(payment, min_interval=interval)
            assert adapter.asked == ["ORD01SYNC"]

            frozen.tick(datetime.timedelta(seconds=6))
            service.sync_payment_status(payment, min_interval=interval)

        assert adapter.asked == ["ORD01SYNC", "ORD01SYNC"]

    def test_without_min_interval_every_call_asks(self, service, adapter, make_payment):
        payment = make_payment()
        adapter.answer = reported(PaymentStatuses.PENDING, "action_required")

        service.sync_payment_status(payment)
        service.sync_payment_status(payment)

        assert len(adapter.asked) == 2


@pytest.mark.django_db
class TestSyncOnRead:
    def test_off_by_default(self, service, adapter, make_payment):
        payment = make_payment()

        assert service.sync_payment_status_on_read(payment) is False
        assert adapter.asked == []

    def test_on_with_an_interval(self, service, adapter, make_payment):
        payment = make_payment()

        with override_settings(VINTA_BILLING={"PAYMENT_STATUS_SYNC": {"ON_READ_INTERVAL": 10}}):
            assert service.sync_payment_status_on_read(payment) is True
            # Within the interval: not asked again (the payment is still the
            # stale in-memory copy, so only the throttle can stop it).
            payment.status = PaymentStatuses.PENDING
            service.sync_payment_status_on_read(payment)

        assert adapter.asked == ["ORD01SYNC"]


@pytest.mark.django_db
class TestSyncOpenPayments:
    def sweep(self, **kwargs):
        dispatched: list[int] = []

        def dispatch(job, payment_id):
            assert job is jobs.sync_payment_status_for_payment
            dispatched.append(payment_id)

        jobs.sync_open_payments(dispatch=dispatch, **kwargs)
        return dispatched

    def test_only_open_payments_with_an_external_id_inside_the_window(self, make_payment):
        hour = datetime.timedelta(hours=1)
        inside = make_payment(age=hour)
        in_process = make_payment(status=PaymentStatuses.IN_PROCESS, age=hour)
        make_payment(age=datetime.timedelta(seconds=30))  # too fresh
        make_payment(age=datetime.timedelta(days=3))  # too old
        make_payment(status=PaymentStatuses.APPROVED, age=hour)
        make_payment(status=PaymentStatuses.PENDING_SEND, external_id="", age=hour)
        make_payment(external_id="", age=hour)

        assert self.sweep() == [inside.pk, in_process.pk]

    def test_the_window_follows_the_settings(self, make_payment):
        fresh = make_payment(age=datetime.timedelta(seconds=30))
        make_payment(age=datetime.timedelta(hours=2))

        with override_settings(
            VINTA_BILLING={"PAYMENT_STATUS_SYNC": {"SWEEP_MIN_AGE": 10, "SWEEP_MAX_AGE": 3600}}
        ):
            assert self.sweep() == [fresh.pk]

    def test_arguments_override_the_settings(self, make_payment):
        old = make_payment(age=datetime.timedelta(days=5))

        assert self.sweep(max_age=datetime.timedelta(days=7)) == [old.pk]

    def test_the_per_payment_job_records_through_the_service(self, service, adapter, make_payment):
        payment = make_payment(age=datetime.timedelta(hours=1))

        jobs.sync_payment_status_for_payment(payment.pk, payment_service=service)

        payment.refresh_from_db()
        assert payment.status == PaymentStatuses.APPROVED

    def test_the_per_payment_job_skips_a_deleted_payment(self, service, adapter):
        jobs.sync_payment_status_for_payment(999_999, payment_service=service)

        assert adapter.asked == []

    def test_the_per_payment_job_does_not_raise(self, make_payment, caplog):
        payment = make_payment()
        broken = Mock()
        broken.sync_payment_status.side_effect = RuntimeError("provider down")

        jobs.sync_payment_status_for_payment(payment.pk, payment_service=broken)

        assert "Status sync failed" in caplog.text


class TestPaymentStatusSyncSettings:
    def test_defaults(self):
        assert get_payment_status_sync_setting("ON_READ_INTERVAL") is None
        assert get_payment_status_sync_setting("SWEEP_MIN_AGE") == datetime.timedelta(minutes=2)
        assert get_payment_status_sync_setting("SWEEP_MAX_AGE") == datetime.timedelta(days=2)

    def test_seconds_become_timedeltas_and_other_keys_keep_defaults(self):
        with override_settings(VINTA_BILLING={"PAYMENT_STATUS_SYNC": {"ON_READ_INTERVAL": 10}}):
            assert get_payment_status_sync_setting("ON_READ_INTERVAL") == datetime.timedelta(
                seconds=10
            )
            assert get_payment_status_sync_setting("SWEEP_MAX_AGE") == datetime.timedelta(days=2)

    @pytest.mark.parametrize(
        "block",
        [
            {"ON_READ_INTERVAL": "10"},
            {"ON_READ_INTERVAL": True},
            {"ON_READ_INTERVAL": 0},
            {"SWEEP_MIN_AGE": None},
            {"SWEEP_MIN_AGE": -1},
            {"SWEEP_MIN_AGE": 3600, "SWEEP_MAX_AGE": 60},
            {"SWEEP_INTERVAL": 60},
            "10 seconds",
        ],
    )
    def test_invalid_values_are_refused(self, block):
        with override_settings(VINTA_BILLING={"PAYMENT_STATUS_SYNC": block}):
            with pytest.raises(ImproperlyConfigured):
                get_payment_status_sync_setting("ON_READ_INTERVAL")
