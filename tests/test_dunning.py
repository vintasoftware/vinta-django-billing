"""The dunning ladder: grace, retries, expiry and recovery.

This is the service that suspends paying customers, so the tests lean on the
cases where being wrong is expensive: a retry firing twice in a bucket (charging
twice), a grace window not expiring on time (free service), and recovery not
clearing the bookkeeping (a cancelled row carrying a stale deadline).
"""

import datetime
from decimal import Decimal

import pytest
from django.test import override_settings
from freezegun import freeze_time

from vinta_billing.constants import BillingState, LimitKind
from vinta_billing.models import BillingPlan, PlanLimit
from vinta_billing.services.dunning_service import (
    FINAL_WARNING_WINDOW,
    MIN_DUNNING_RETRY_INTERVAL,
    DunningService,
    dunning_retry_idempotency_key,
    grace_period_days,
    is_downgrade_grace,
    retry_attempt_ordinal,
)
from vinta_billing.services.subscription_service import retry_payment_idempotency_key
from tests.testapp.models import Widget


pytestmark = pytest.mark.django_db


class Recorder:
    def __init__(self):
        self.calls = []

    def create_notification(self, **kwargs):
        self.calls.append(kwargs)


class FakeSubscriptionService:
    """Records retries instead of driving a provider."""

    def __init__(self):
        self.retries = []

    def retry_failed_charge(self, subscription, idempotency_key):
        self.retries.append((subscription.pk, idempotency_key))


@pytest.fixture
def notifier():
    return Recorder()


@pytest.fixture
def subscription_service():
    return FakeSubscriptionService()


@pytest.fixture
def service(notifier, subscription_service):
    return DunningService(subscription_service=subscription_service, notification_service=notifier)


@pytest.fixture
def free_plan(db):
    """The plan an expired grace falls back to when usage fits under it."""
    plan = BillingPlan.objects.create(
        name="Free",
        slug="free",
        monthly_price=Decimal("0.00"),
        annual_price=Decimal("0.00"),
        is_active=True,
    )
    for key, kind, value in (
        ("widgets", LimitKind.PREPAID, 1),
        ("seats", LimitKind.PREPAID, 1),
        ("event_occurrences", LimitKind.POSTPAID, 0),
    ):
        PlanLimit.objects.create(plan=plan, resource_key=key, limit_value=value, kind=kind)
    return plan


class TestGracePeriodDays:
    def test_the_plan_wins_when_it_sets_one(self, subscription):
        subscription.plan.grace_period_days = 3
        subscription.plan.save(update_fields=["grace_period_days"])

        assert grace_period_days(subscription) == 3

    def test_falls_back_to_the_configured_default(self, subscription):
        subscription.plan.grace_period_days = None
        subscription.plan.save(update_fields=["grace_period_days"])

        with override_settings(VINTA_BILLING={"GRACE_PERIOD_DAYS": 21}):
            assert grace_period_days(subscription) == 21


class TestRetryBuckets:
    def test_no_grace_deadline_is_bucket_zero(self, subscription):
        subscription.grace_period_ends_at = None

        assert retry_attempt_ordinal(subscription, datetime.datetime.now(datetime.UTC)) == 0

    def test_the_bucket_advances_once_per_interval(self, subscription):
        subscription.plan.grace_period_days = 7
        subscription.plan.save(update_fields=["grace_period_days"])
        grace_start = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        subscription.grace_period_ends_at = grace_start + datetime.timedelta(days=7)

        assert retry_attempt_ordinal(subscription, grace_start) == 0
        assert retry_attempt_ordinal(subscription, grace_start + MIN_DUNNING_RETRY_INTERVAL) == 1
        assert (
            retry_attempt_ordinal(subscription, grace_start + MIN_DUNNING_RETRY_INTERVAL * 3) == 3
        )

    def test_the_two_idempotency_namespaces_can_never_collide(self):
        """A ladder retry and a user-triggered retry must never share a key --
        the provider would refuse the second as a duplicate of the first, and
        the customer's manual retry would silently do nothing.
        """
        ladder_keys = {dunning_retry_idempotency_key(1, ordinal) for ordinal in range(50)}
        manual_keys = {retry_payment_idempotency_key(1, str(n)) for n in range(50)}

        assert ladder_keys & manual_keys == set()


class TestEnterGrace:
    def test_moves_an_active_subscription_into_grace(self, service, subscription):
        subscription.billing_state = BillingState.ACTIVE
        subscription.save(update_fields=["billing_state"])

        service.enter_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE
        assert subscription.grace_period_ends_at is not None

    def test_stamps_the_deadline_from_the_plans_window(self, service, subscription):
        subscription.plan.grace_period_days = 5
        subscription.plan.save(update_fields=["grace_period_days"])
        subscription.billing_state = BillingState.ACTIVE
        subscription.save(update_fields=["billing_state"])

        with freeze_time("2026-03-01T00:00:00Z"):
            service.enter_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.grace_period_ends_at == datetime.datetime(
            2026, 3, 6, tzinfo=datetime.UTC
        )

    @pytest.mark.parametrize(
        "state", [BillingState.GRACE, BillingState.RESTRICTED, BillingState.CANCELLED]
    )
    def test_a_redelivered_failure_is_a_no_op(self, service, subscription, state):
        """A provider can legitimately report another failure while the ladder is
        already further along. Raising on that timing race would turn a normal
        redelivery into a 500 on the webhook.
        """
        subscription.billing_state = state
        subscription.save(update_fields=["billing_state"])

        service.enter_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == state

    def test_clears_a_pending_plan_change_confirmation(self, service, subscription):
        """A failed first-upgrade charge must not leave the organization stuck
        waiting for a confirmation that will never come."""
        subscription.billing_state = BillingState.FREE
        subscription.plan_change_pending_confirmation = True
        subscription.save(update_fields=["billing_state", "plan_change_pending_confirmation"])

        service.enter_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.plan_change_pending_confirmation is False


class TestResolvePaymentSuccess:
    @pytest.mark.parametrize("state", [BillingState.GRACE, BillingState.RESTRICTED])
    def test_a_confirmed_charge_restores_active(self, service, subscription, state):
        subscription.billing_state = state
        subscription.grace_period_ends_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        subscription.last_dunning_attempt_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        subscription.save(
            update_fields=["billing_state", "grace_period_ends_at", "last_dunning_attempt_at"]
        )

        service.resolve_payment_success(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.ACTIVE

    def test_it_clears_the_dunning_bookkeeping(self, service, subscription):
        """A recovered row must not keep a deadline the sweep could act on."""
        subscription.billing_state = BillingState.GRACE
        subscription.grace_period_ends_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        subscription.last_dunning_attempt_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        subscription.save(
            update_fields=["billing_state", "grace_period_ends_at", "last_dunning_attempt_at"]
        )

        service.resolve_payment_success(subscription)

        subscription.refresh_from_db()
        assert subscription.grace_period_ends_at is None
        assert subscription.last_dunning_attempt_at is None

    @pytest.mark.parametrize(
        "state", [BillingState.ACTIVE, BillingState.FREE, BillingState.CANCELLED]
    )
    def test_other_states_are_untouched(self, service, subscription, state):
        subscription.billing_state = state
        subscription.save(update_fields=["billing_state"])

        service.resolve_payment_success(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == state


class TestProcessGrace:
    def _in_grace(self, subscription, *, started, days=7):
        subscription.plan.grace_period_days = days
        subscription.plan.save(update_fields=["grace_period_days"])
        subscription.billing_state = BillingState.GRACE
        subscription.grace_period_ends_at = started + datetime.timedelta(days=days)
        subscription.last_dunning_attempt_at = None
        subscription.save(
            update_fields=["billing_state", "grace_period_ends_at", "last_dunning_attempt_at"]
        )
        return subscription

    def test_a_tick_inside_the_window_retries_the_charge(
        self, service, subscription_service, subscription
    ):
        started = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        self._in_grace(subscription, started=started)

        with freeze_time(started + datetime.timedelta(hours=1)):
            service.process_subscription(subscription)

        assert len(subscription_service.retries) == 1

    def test_a_second_tick_in_the_same_bucket_does_not_retry_again(
        self, service, subscription_service, subscription
    ):
        """Two beat ticks an hour apart are the same logical attempt. Charging
        twice for it is the expensive failure this throttle prevents."""
        started = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        self._in_grace(subscription, started=started)

        with freeze_time(started + datetime.timedelta(hours=1)):
            service.process_subscription(subscription)
        subscription.refresh_from_db()
        with freeze_time(started + datetime.timedelta(hours=2)):
            service.process_subscription(subscription)

        assert len(subscription_service.retries) == 1

    def test_the_next_bucket_retries_again(self, service, subscription_service, subscription):
        started = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        self._in_grace(subscription, started=started)

        with freeze_time(started + datetime.timedelta(hours=1)):
            service.process_subscription(subscription)
        subscription.refresh_from_db()
        with freeze_time(started + MIN_DUNNING_RETRY_INTERVAL + datetime.timedelta(hours=1)):
            service.process_subscription(subscription)

        assert len(subscription_service.retries) == 2

    def test_each_attempt_carries_its_own_bucket_key(
        self, service, subscription_service, subscription
    ):
        started = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        self._in_grace(subscription, started=started)

        with freeze_time(started + datetime.timedelta(hours=1)):
            service.process_subscription(subscription)
        subscription.refresh_from_db()
        with freeze_time(started + MIN_DUNNING_RETRY_INTERVAL + datetime.timedelta(hours=1)):
            service.process_subscription(subscription)

        keys = [key for _pk, key in subscription_service.retries]
        assert len(set(keys)) == 2

    def test_expiry_is_checked_on_every_tick_not_only_a_retry_tick(
        self, service, subscription, free_plan
    ):
        """The throttle covers the retry, never the expiry check. Throttling the
        whole method would let a window run up to a day past its deadline.
        """
        started = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        self._in_grace(subscription, started=started)
        subscription.last_dunning_attempt_at = started + datetime.timedelta(days=7)
        subscription.save(update_fields=["last_dunning_attempt_at"])

        with freeze_time(started + datetime.timedelta(days=7, minutes=1)):
            service.process_subscription(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state != BillingState.GRACE

    def test_a_downgrade_grace_is_never_retried(
        self, service, subscription_service, subscription, free_plan
    ):
        """There is no failed charge to retry, and driving one would bill the
        still-active higher plan while the webhook syncs the lower one."""
        started = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        self._in_grace(subscription, started=started)
        subscription.pending_plan = free_plan
        subscription.save(update_fields=["pending_plan"])
        assert is_downgrade_grace(subscription)

        with freeze_time(started + datetime.timedelta(hours=1)):
            service.process_subscription(subscription)

        assert subscription_service.retries == []

    def test_the_reminder_is_sent_after_the_retry_commits(
        self, service, notifier, subscription, membership, django_capture_on_commit_callbacks
    ):
        """Queued through `on_commit`, so a rolled-back retry never emails the
        customer about a charge that did not happen."""
        started = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        self._in_grace(subscription, started=started)
        deadline = started + datetime.timedelta(days=7)

        with freeze_time(deadline - FINAL_WARNING_WINDOW + datetime.timedelta(minutes=1)):
            with django_capture_on_commit_callbacks(execute=True):
                service.process_subscription(subscription)

        assert len(notifier.calls) == 1

    def test_no_reminder_is_sent_when_the_tick_is_throttled(
        self, service, notifier, subscription, membership, django_capture_on_commit_callbacks
    ):
        started = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        self._in_grace(subscription, started=started)

        with freeze_time(started + datetime.timedelta(hours=1)):
            with django_capture_on_commit_callbacks(execute=True):
                service.process_subscription(subscription)
        subscription.refresh_from_db()
        notifier.calls.clear()
        with freeze_time(started + datetime.timedelta(hours=2)):
            with django_capture_on_commit_callbacks(execute=True):
                service.process_subscription(subscription)

        assert notifier.calls == []


class TestExpireGrace:
    def test_usage_over_the_free_ceiling_is_restricted(
        self, service, subscription, organization, free_plan
    ):
        Widget.objects.create(organization=organization, name="a")
        Widget.objects.create(organization=organization, name="b")  # free plan allows 1
        subscription.billing_state = BillingState.GRACE
        subscription.save(update_fields=["billing_state"])

        service.expire_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.RESTRICTED

    def test_usage_under_the_free_ceiling_falls_back_to_free(
        self, service, subscription, free_plan
    ):
        """An organization that already fits the free plan is not suspended."""
        subscription.billing_state = BillingState.GRACE
        subscription.save(update_fields=["billing_state"])

        service.expire_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.FREE

    def test_the_fallback_gates_state_only_and_leaves_the_plan_alone(
        self, service, subscription, plan, free_plan
    ):
        """Deliberate, and easy to mistake for a bug: `billing_state` and `plan`
        are allowed to disagree, the same way they do for the length of a
        downgrade's grace window. Whether the nominal plan should also snap to
        free is left as a product decision.
        """
        subscription.billing_state = BillingState.GRACE
        subscription.save(update_fields=["billing_state"])

        service.expire_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.FREE
        assert subscription.plan == plan

    def test_the_fallback_clears_the_dunning_bookkeeping(self, service, subscription, free_plan):
        subscription.billing_state = BillingState.GRACE
        subscription.grace_period_ends_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        subscription.save(update_fields=["billing_state", "grace_period_ends_at"])

        service.expire_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.grace_period_ends_at is None

    def test_with_no_free_plan_in_the_catalog_it_restricts(self, service, subscription):
        """Nowhere to fall back to, so the safe outcome is to block writes rather
        than leave an unpaid organization on a paid plan."""
        subscription.billing_state = BillingState.GRACE
        subscription.save(update_fields=["billing_state"])

        service.expire_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.RESTRICTED


class TestRestrictedSweep:
    def test_a_restricted_organization_that_now_fits_free_is_released(
        self, service, subscription, free_plan
    ):
        """Deleting resources is how a restricted customer digs themselves out
        without paying; the sweep has to notice."""
        subscription.billing_state = BillingState.RESTRICTED
        subscription.save(update_fields=["billing_state"])

        service.process_subscription(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.FREE

    def test_a_restricted_organization_still_over_the_ceiling_stays_put(
        self, service, subscription, organization, free_plan
    ):
        Widget.objects.create(organization=organization, name="a")
        Widget.objects.create(organization=organization, name="b")
        subscription.billing_state = BillingState.RESTRICTED
        subscription.save(update_fields=["billing_state"])

        service.process_subscription(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.RESTRICTED


class TestDefaultCollaborators:
    def test_the_service_builds_bare(self):
        """Constructed with nothing, it must still resolve its collaborators."""
        service = DunningService()

        assert service.subscription_service is not None
        assert service.entitlement_service is not None
        assert service.notification_service is not None
