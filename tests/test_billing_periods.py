"""Cycle boundary arithmetic.

Everything money lands on is resolved here: the meter stamps an occurrence's
period from it, the usage counter reads rows back by it, and cycle close
recomputes a settled cycle's bounds with it. Three places that must agree, so
these tests pin the agreement rather than any one caller's use of it.
"""

import datetime
from decimal import Decimal

import pytest
from dateutil.relativedelta import relativedelta
from django.utils import timezone
from freezegun import freeze_time

from vinta_billing.constants import BillingInterval, LimitKind
from vinta_billing.exceptions import BillingPeriodResolutionError, IncompleteBillingPlanError
from vinta_billing.models import BillingPlan, PlanLimit
from vinta_billing.services.subscription_service import (
    MAX_BILLING_PERIOD_STEPS,
    assert_plan_is_complete,
    billing_interval_step,
    current_billing_period_start,
    overage_settlement_step,
    resolve_billing_period,
    resolve_billing_period_start,
    resolve_settlement_period,
    retry_payment_idempotency_key,
)


pytestmark = pytest.mark.django_db


def utc(year, month, day, hour=0):
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.UTC)


@pytest.fixture
def monthly(organization, plan, make_subscription):
    """A subscription whose stored period is March 2026."""
    return make_subscription(
        organization,
        plan,
        billing_interval=BillingInterval.MONTHLY,
        current_period_start=utc(2026, 3, 1),
        current_period_end=utc(2026, 4, 1),
    )


@pytest.fixture
def annual(organization, plan, make_subscription):
    """An annually-billed subscription, stored period one *month* long.

    That is not a mistake: subscriptions are created with a one-month stored
    period and cycle close rolls it forward monthly whatever the plan's billing
    interval, because overage settles monthly for every plan.
    """
    return make_subscription(
        organization,
        plan,
        billing_interval=BillingInterval.ANNUAL,
        current_period_start=utc(2026, 3, 1),
        current_period_end=utc(2026, 4, 1),
    )


class TestSteps:
    def test_monthly_is_one_month(self):
        assert billing_interval_step(BillingInterval.MONTHLY) == relativedelta(months=1)

    def test_annual_is_one_year(self):
        assert billing_interval_step(BillingInterval.ANNUAL) == relativedelta(years=1)

    def test_an_unknown_interval_falls_back_to_monthly(self):
        """Fails towards the shorter cycle: a subscription billed too often is a
        visible support problem, one billed too rarely is silent lost revenue."""
        assert billing_interval_step("fortnightly") == relativedelta(months=1)

    def test_settlement_is_always_monthly(self):
        """Overage settles monthly for every plan, annual ones included."""
        assert overage_settlement_step() == relativedelta(months=1)


class TestResolveBillingPeriod:
    def test_a_moment_inside_the_stored_period_resolves_to_it(self, monthly):
        assert resolve_billing_period(monthly, utc(2026, 3, 15)) == (
            utc(2026, 3, 1),
            utc(2026, 4, 1),
        )

    def test_the_start_boundary_is_inclusive(self, monthly):
        assert resolve_billing_period(monthly, utc(2026, 3, 1))[0] == utc(2026, 3, 1)

    def test_the_end_boundary_belongs_to_the_next_cycle(self, monthly):
        """Half-open on purpose: an occurrence starting exactly at the boundary is
        billed once, in the next cycle -- not twice, and not never."""
        assert resolve_billing_period(monthly, utc(2026, 4, 1)) == (
            utc(2026, 4, 1),
            utc(2026, 5, 1),
        )

    def test_steps_forward_into_a_future_cycle(self, monthly):
        assert resolve_billing_period(monthly, utc(2026, 6, 10)) == (
            utc(2026, 6, 1),
            utc(2026, 7, 1),
        )

    def test_steps_backward_into_a_past_cycle(self, monthly):
        assert resolve_billing_period(monthly, utc(2025, 12, 10)) == (
            utc(2025, 12, 1),
            utc(2026, 1, 1),
        )

    def test_an_annual_plan_steps_by_years(self, annual):
        """The plan-cycle stride, which is what the *fee* cycle uses.

        Note where it lands. The stored period is one month long (2026-03-01 to
        2026-04-01) but the stride is a year, so the first step forward produces
        2026-04-01 to 2027-04-01 -- a period anchored on the stored *end*, not on
        the stored start. That is the documented consequence of reconstructing
        neighbours from the current anchor when the stored period is not one
        stride long, and it is exactly why settlement periods are resolved with
        their own monthly stride instead of this one.
        """
        assert resolve_billing_period(annual, utc(2027, 3, 15)) == (
            utc(2026, 4, 1),
            utc(2027, 4, 1),
        )

    def test_an_unreachable_moment_raises_rather_than_spinning(self, monthly):
        """A corrupt period pair would otherwise loop for a very long time."""
        far_future = utc(2026, 3, 1) + relativedelta(months=MAX_BILLING_PERIOD_STEPS + 5)

        with pytest.raises(BillingPeriodResolutionError):
            resolve_billing_period(monthly, far_future)

    def test_a_month_end_anchor_does_not_drift(self, organization, plan, make_subscription):
        """A cycle anchored on the 31st must not walk backwards a day a month.

        `relativedelta` clamps into short months and restores the anchor after;
        `timedelta(days=30)` would not, and a year of that moves the invoice date
        by nearly a week.
        """
        subscription = make_subscription(
            organization,
            plan,
            current_period_start=utc(2026, 1, 31),
            current_period_end=utc(2026, 2, 28),
        )

        # February clamps to the 28th, then March restores the 31st.
        assert resolve_billing_period(subscription, utc(2026, 3, 15)) == (
            utc(2026, 2, 28),
            utc(2026, 3, 28),
        )


class TestSettlementPeriod:
    def test_an_annual_plans_history_is_walked_monthly(self, annual):
        """This is the distinction the two resolvers exist for.

        Cycle close rolls the stored period forward one month whatever the
        billing interval, so an annual plan's past periods are monthly. Walking
        them with the twelve-month plan stride lands on bounds no row was ever
        stamped with.
        """
        assert resolve_settlement_period(annual, utc(2026, 1, 15)) == (
            utc(2026, 1, 1),
            utc(2026, 2, 1),
        )

    def test_it_differs_from_the_plan_cycle_for_an_annual_plan(self, annual):
        moment = utc(2026, 1, 15)

        assert resolve_settlement_period(annual, moment) != resolve_billing_period(annual, moment)

    def test_it_agrees_with_the_plan_cycle_for_a_monthly_plan(self, monthly):
        moment = utc(2026, 1, 15)

        assert resolve_settlement_period(monthly, moment) == resolve_billing_period(monthly, moment)


class TestCurrentPeriod:
    def test_start_is_the_first_element_of_the_pair(self, monthly):
        moment = utc(2026, 5, 9)

        assert (
            resolve_billing_period_start(monthly, moment)
            == resolve_billing_period(monthly, moment)[0]
        )

    @freeze_time("2026-07-09T12:00:00Z")
    def test_current_is_derived_from_now_not_from_the_stored_column(self, monthly):
        """The stored column records the cycle the subscription last advanced
        into. Reading it directly is the bug this replaced: once the stored
        period elapses, the meter writes one period and the counter asks for an
        earlier one, and the counter reads zero forever.
        """
        assert monthly.current_period_start == utc(2026, 3, 1)
        assert current_billing_period_start(monthly) == utc(2026, 7, 1)

    @freeze_time("2026-03-15T12:00:00Z")
    def test_current_matches_the_stored_column_while_it_is_still_live(self, monthly):
        assert current_billing_period_start(monthly) == monthly.current_period_start

    def test_current_tracks_the_clock(self, monthly):
        """Sanity check against a frozen-clock artefact: unfrozen, the answer is
        whatever cycle contains the real now."""
        start = current_billing_period_start(monthly)

        assert start <= timezone.now() < start + relativedelta(months=1)


class TestPlanCompleteness:
    def test_a_plan_covering_every_registered_resource_passes(self, plan):
        assert assert_plan_is_complete(plan) is None

    def test_a_plan_missing_a_resource_is_refused(self, db):
        """An omitted resource reads as *unlimited*, so a downgrade onto an
        incomplete plan would grant an infinite ceiling -- the exact inverse of a
        downgrade."""
        incomplete = BillingPlan.objects.create(
            name="Incomplete",
            slug="incomplete",
            monthly_price=Decimal("1.00"),
            annual_price=Decimal("10.00"),
            is_active=True,
        )
        PlanLimit.objects.create(
            plan=incomplete, resource_key="widgets", limit_value=1, kind=LimitKind.PREPAID
        )

        with pytest.raises(IncompleteBillingPlanError):
            assert_plan_is_complete(incomplete)

    def test_the_error_names_what_is_missing(self, db):
        incomplete = BillingPlan.objects.create(
            name="Incomplete2",
            slug="incomplete2",
            monthly_price=Decimal("1.00"),
            annual_price=Decimal("10.00"),
            is_active=True,
        )

        with pytest.raises(IncompleteBillingPlanError) as excinfo:
            assert_plan_is_complete(incomplete)

        assert "widgets" in str(excinfo.value)


class TestIdempotencyKey:
    def test_it_is_namespaced_by_subscription_and_client_key(self):
        assert retry_payment_idempotency_key(7, "abc") == "retry-payment-7-abc"

    def test_two_subscriptions_never_collide_on_the_same_client_key(self):
        assert retry_payment_idempotency_key(1, "k") != retry_payment_idempotency_key(2, "k")
