"""Closing a billing period: rolling the window, settling overage, writing the
statement.

Two properties dominate: closing must be idempotent (a re-run after a completed
close must not charge again), and it must catch up when a sweep has been missed
for several periods without collapsing them into one.
"""

import datetime
from decimal import Decimal

import pytest
from freezegun import freeze_time

from vinta_billing.constants import BillingInterval, BillingState, LimitKind
from vinta_billing.models import BillingPeriodSummary, MeteredOccurrence
from vinta_billing.services.cycle_close_service import MAX_CLOSE_PERIODS_PER_RUN, CycleCloseService


pytestmark = pytest.mark.django_db


def utc(year, month, day):
    return datetime.datetime(year, month, day, tzinfo=datetime.UTC)


class FakePaymentService:
    """Records overage charges instead of driving a provider.

    Returns a real `Payment` row rather than a stub: cycle close stores it on
    the statement, so a double would have to satisfy a foreign key anyway.
    """

    def __init__(self, billing_profile=None):
        self.charges = []
        self.billing_profile = billing_profile

    def create_payment(self, **kwargs):
        from vinta_billing.models import Payment

        self.charges.append(kwargs)
        return Payment.objects.create(
            billing_profile=self.billing_profile,
            value=kwargs["amount"],
            currency=kwargs["currency"],
            payment_provider="stripe",
            external_id="ext-%d" % len(self.charges),
            status="approved",
            original_status="approved",
            payment_method=kwargs["payment_method"],
            description=kwargs["description"],
        )


@pytest.fixture
def payment_service(billing_profile):
    return FakePaymentService(billing_profile)


@pytest.fixture
def service(payment_service):
    return CycleCloseService(payment_service=payment_service)


@pytest.fixture
def closable(organization, plan, make_subscription):
    """A subscription whose stored period ended on 1 April 2026."""
    plan.limits.filter(resource_key="event_occurrences").update(
        limit_value=1, overage_unit_price=Decimal("0.50")
    )
    return make_subscription(
        organization,
        plan,
        billing_interval=BillingInterval.MONTHLY,
        billing_state=BillingState.ACTIVE,
        current_period_start=utc(2026, 3, 1),
        current_period_end=utc(2026, 4, 1),
    )


def record_occurrences(subscription, organization, count, *, period_start, within_allowance=False):
    for index in range(count):
        MeteredOccurrence.objects.create(
            organization=organization,
            subscription=subscription,
            event_id=index + 1,
            occurrence_start=period_start + datetime.timedelta(days=index + 1),
            billing_period_start=period_start,
            is_within_allowance=within_allowance,
            unit_price=Decimal("0") if within_allowance else Decimal("0.50"),
        )


class TestRolling:
    def test_nothing_closes_before_the_period_ends(self, service, closable):
        closed = service.close_subscription(closable, now=utc(2026, 3, 15))

        assert closed == []

    def test_an_elapsed_period_is_rolled_forward(self, service, closable):
        closed = service.close_subscription(closable, now=utc(2026, 4, 2))

        closable.refresh_from_db()
        assert len(closed) == 1
        assert closable.current_period_start == utc(2026, 4, 1)
        assert closable.current_period_end == utc(2026, 5, 1)

    def test_the_closed_period_reports_its_own_bounds(self, service, closable):
        closed = service.close_subscription(closable, now=utc(2026, 4, 2))

        assert closed[0].billing_period_start == utc(2026, 3, 1)
        assert closed[0].billing_period_end == utc(2026, 4, 1)

    def test_a_second_close_is_a_no_op(self, service, closable):
        """The re-run safety property: the rolled period is in the future, so the
        loop guard exits immediately and nothing is charged twice."""
        service.close_subscription(closable, now=utc(2026, 4, 2))
        closable.refresh_from_db()

        second = service.close_subscription(closable, now=utc(2026, 4, 2))

        assert second == []

    def test_several_missed_periods_close_one_by_one(self, service, closable):
        """A sweep down for three months must settle three periods, not merge
        them into one -- each has its own usage and its own statement."""
        closed = service.close_subscription(closable, now=utc(2026, 7, 2))

        assert [period.billing_period_start for period in closed] == [
            utc(2026, 3, 1),
            utc(2026, 4, 1),
            utc(2026, 5, 1),
            utc(2026, 6, 1),
        ]

    def test_catch_up_is_bounded(self, service, organization, plan, make_subscription):
        """A corrupt period far in the past must not spin forever; the run stops
        at the cap and the next run continues from there."""
        subscription = make_subscription(
            organization,
            plan,
            current_period_start=utc(1990, 1, 1),
            current_period_end=utc(1990, 2, 1),
        )

        closed = service.close_subscription(subscription, now=utc(2026, 4, 2))

        assert len(closed) == MAX_CLOSE_PERIODS_PER_RUN


class TestStatements:
    def test_a_statement_is_written_for_the_closed_period(self, service, closable):
        service.close_subscription(closable, now=utc(2026, 4, 2))

        summary = BillingPeriodSummary.objects.get()
        assert summary.billing_period_start == utc(2026, 3, 1)
        assert summary.billing_period_end == utc(2026, 4, 1)

    def test_the_statement_records_the_overage_total(self, service, closable, organization):
        record_occurrences(closable, organization, 4, period_start=utc(2026, 3, 1))

        service.close_subscription(closable, now=utc(2026, 4, 2))

        summary = BillingPeriodSummary.objects.get()
        assert summary.overage_total == Decimal("2.00")

    def test_allowance_occurrences_cost_nothing(self, service, closable, organization):
        record_occurrences(
            closable, organization, 3, period_start=utc(2026, 3, 1), within_allowance=True
        )

        service.close_subscription(closable, now=utc(2026, 4, 2))

        assert BillingPeriodSummary.objects.get().overage_total == Decimal("0")

    def test_each_closed_period_gets_its_own_statement(self, service, closable):
        service.close_subscription(closable, now=utc(2026, 6, 2))

        assert BillingPeriodSummary.objects.count() == 3

    def test_re_closing_does_not_duplicate_the_statement(self, service, closable):
        service.close_subscription(closable, now=utc(2026, 4, 2))
        closable.refresh_from_db()
        service.close_subscription(closable, now=utc(2026, 4, 2))

        assert BillingPeriodSummary.objects.count() == 1


class TestOverageCharging:
    def test_no_overage_means_no_charge(self, service, payment_service, closable):
        service.close_subscription(closable, now=utc(2026, 4, 2))

        assert payment_service.charges == []

    def test_accrued_overage_is_charged_once(
        self, service, payment_service, closable, organization
    ):
        record_occurrences(closable, organization, 3, period_start=utc(2026, 3, 1))

        service.close_subscription(closable, now=utc(2026, 4, 2))

        assert len(payment_service.charges) == 1
        assert payment_service.charges[0]["amount"] == Decimal("1.50")

    def test_the_charge_carries_a_period_scoped_idempotency_key(
        self, service, payment_service, closable, organization
    ):
        """Re-running a close that crashed after charging must not charge again;
        the provider dedups on this key."""
        record_occurrences(closable, organization, 3, period_start=utc(2026, 3, 1))

        service.close_subscription(closable, now=utc(2026, 4, 2))

        assert "2026-03-01" in payment_service.charges[0]["idempotency_key"]

    def test_an_unlimited_allowance_charges_nothing(
        self, service, payment_service, organization, plan, make_subscription
    ):
        """Every organization is on an unlimited allowance during a rollout, so
        this is the branch that actually runs in production today."""
        plan.limits.filter(resource_key="event_occurrences").update(limit_value=None)
        subscription = make_subscription(
            organization,
            plan,
            current_period_start=utc(2026, 3, 1),
            current_period_end=utc(2026, 4, 1),
        )
        record_occurrences(subscription, organization, 5, period_start=utc(2026, 3, 1))

        service.close_subscription(subscription, now=utc(2026, 4, 2))

        assert payment_service.charges == []

    def test_the_closed_period_reports_whether_it_charged(self, service, closable, organization):
        record_occurrences(closable, organization, 2, period_start=utc(2026, 3, 1))

        closed = service.close_subscription(closable, now=utc(2026, 4, 2))

        assert closed[0].overage_total == Decimal("1.00")


class TestDueForClose:
    def test_a_subscription_past_its_period_end_is_due(self, service, closable):
        due = service.subscriptions_to_close(now=utc(2026, 4, 2))

        assert closable.pk in due

    def test_a_subscription_inside_its_period_is_not(self, service, closable):
        due = service.subscriptions_to_close(now=utc(2026, 3, 15))

        assert closable.pk not in due

    @freeze_time("2026-04-02T00:00:00Z")
    def test_now_defaults_to_the_clock(self, service, closable):
        assert closable.pk in service.subscriptions_to_close()


class TestPeriodBoundaryActions:
    def test_a_cancelled_subscription_reverts_to_free_at_the_boundary(self, service, closable):
        """The spec's "runs to the end of the paid cycle, then reverts to FREE"
        lifecycle -- the cancel action only moves the state, close applies it."""
        closable.billing_state = BillingState.CANCELLED
        closable.save(update_fields=["billing_state"])

        service.close_subscription(closable, now=utc(2026, 4, 2))

        closable.refresh_from_db()
        assert closable.billing_state == BillingState.FREE

    def test_an_active_subscription_stays_active(self, service, closable):
        service.close_subscription(closable, now=utc(2026, 4, 2))

        closable.refresh_from_db()
        assert closable.billing_state == BillingState.ACTIVE


class TestDefaultCollaborators:
    def test_the_service_builds_bare(self):
        service = CycleCloseService()

        assert service._metering_service is not None
        assert service._subscription_service is not None
        assert service._entitlement_service is not None


class TestPostpaidPlanShape:
    def test_a_plan_with_no_postpaid_limit_still_closes(
        self, service, organization, plan, make_subscription
    ):
        """Not every project meters anything; close must not require it."""
        plan.limits.filter(kind=LimitKind.POSTPAID).delete()
        subscription = make_subscription(
            organization,
            plan,
            current_period_start=utc(2026, 3, 1),
            current_period_end=utc(2026, 4, 1),
        )

        closed = service.close_subscription(subscription, now=utc(2026, 4, 2))

        assert len(closed) == 1
