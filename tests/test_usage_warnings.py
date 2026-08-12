"""Approaching-limit and limit-reached warnings.

Debouncing is the whole point of this service: the beat sweep re-checks every
subscription on every tick, so without the marker an organization sitting at 85%
of its seat limit would be told so every few minutes for a month.
"""

import datetime
from decimal import Decimal

import pytest
from django.test import override_settings
from freezegun import freeze_time

from billing.constants import BillingState, LimitWarningLevel
from billing.models import LimitWarningNotification
from billing.services.usage_warning_service import (
    UsageWarningService,
)
from tests.testapp.models import Widget


pytestmark = pytest.mark.django_db


class Recorder:
    def __init__(self):
        self.calls = []

    def create_notification(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture
def notifier():
    return Recorder()


@pytest.fixture
def service(notifier):
    return UsageWarningService(notification_service=notifier)


def make_widgets(organization, count):
    for index in range(count):
        Widget.objects.create(organization=organization, name="w%d" % index)


class TestLevelThresholds:
    def test_at_the_threshold_it_is_approaching(self):
        assert UsageWarningService._level_for(8, 10) == LimitWarningLevel.APPROACHING

    def test_just_below_the_threshold_is_silent(self):
        assert UsageWarningService._level_for(7, 10) is None

    def test_at_the_ceiling_it_is_reached(self):
        assert UsageWarningService._level_for(10, 10) == LimitWarningLevel.REACHED

    def test_over_the_ceiling_is_still_reached(self):
        assert UsageWarningService._level_for(15, 10) == LimitWarningLevel.REACHED

    def test_any_usage_against_a_zero_ceiling_has_reached_it(self):
        """`limit_value=0` means "not included", so one unit is already over."""
        assert UsageWarningService._level_for(1, 0) == LimitWarningLevel.REACHED

    def test_no_usage_against_a_zero_ceiling_is_silent(self):
        """A resource the organization has never touched must not be warned
        about."""
        assert UsageWarningService._level_for(0, 0) is None

    def test_the_ratio_is_exact_not_floating_point(self):
        """Decimal, so a limit of 3 at 80% does not land on the wrong side of
        the boundary through binary rounding."""
        assert UsageWarningService._ratio(1, 3) == Decimal(1) / Decimal(3)


class TestCheckSubscription:
    def test_warns_when_usage_crosses_the_threshold(
        self, service, notifier, organization, subscription, membership
    ):
        make_widgets(organization, 3)  # ceiling 3 -> reached

        service.check_subscription(subscription)

        assert len(notifier.calls) == 1
        assert notifier.calls[0]["context_kwargs"]["resource_key"] == "widgets"

    def test_stays_silent_below_the_threshold(
        self, service, notifier, organization, subscription, membership
    ):
        make_widgets(organization, 1)  # 1/3 = 33%

        service.check_subscription(subscription)

        assert notifier.calls == []

    def test_writes_a_marker_so_the_next_tick_is_debounced(
        self, service, notifier, organization, subscription, membership
    ):
        make_widgets(organization, 3)

        service.check_subscription(subscription)
        service.check_subscription(subscription)

        assert len(notifier.calls) == 1
        assert (
            LimitWarningNotification.objects.filter(
                subscription=subscription, resource_key="widgets"
            ).count()
            == 1
        )

    def test_approaching_and_reached_debounce_independently(
        self, service, notifier, organization, plan, make_subscription, membership
    ):
        """An organization gets exactly one "you're close" and, separately, one
        "you're at your limit" per resource per cycle -- not one or the other.

        Needs a ceiling of 10: with a ceiling of 3 the approaching band
        (80% up to but excluding 100%) spans 2.4 to 3, which contains no integer,
        so usage jumps straight from silent to reached.
        """
        plan.limits.filter(resource_key="widgets").update(limit_value=10)
        subscription = make_subscription(organization, plan)
        make_widgets(organization, 8)

        service.check_subscription(subscription)
        approaching = list(notifier.calls)

        make_widgets(organization, 2)
        service.check_subscription(subscription)

        levels = set(
            LimitWarningNotification.objects.filter(resource_key="widgets").values_list(
                "level", flat=True
            )
        )
        assert len(approaching) == 1
        assert levels == {LimitWarningLevel.APPROACHING, LimitWarningLevel.REACHED}
        assert len(notifier.calls) == 2

    def test_a_second_tick_at_the_same_level_stays_silent(
        self, service, notifier, organization, plan, make_subscription, membership
    ):
        """The marker is per (resource, level, cycle), so re-checking while still
        approaching sends nothing further."""
        plan.limits.filter(resource_key="widgets").update(limit_value=10)
        subscription = make_subscription(organization, plan)
        make_widgets(organization, 8)

        service.check_subscription(subscription)
        make_widgets(organization, 1)  # still approaching, 9/10
        service.check_subscription(subscription)

        assert len(notifier.calls) == 1

    def test_a_new_billing_period_warns_again(
        self, service, notifier, organization, plan, make_subscription, membership
    ):
        """The debounce is per cycle, not forever."""
        subscription = make_subscription(
            organization,
            plan,
            current_period_start=datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC),
            current_period_end=datetime.datetime(2026, 4, 1, tzinfo=datetime.UTC),
        )
        make_widgets(organization, 3)

        with freeze_time("2026-03-15T00:00:00Z"):
            service.check_subscription(subscription)
        with freeze_time("2026-04-15T00:00:00Z"):
            service.check_subscription(subscription)

        assert len(notifier.calls) == 2

    def test_an_unlimited_resource_never_warns(
        self, service, notifier, organization, unlimited_plan, make_subscription, membership
    ):
        subscription = make_subscription(organization, unlimited_plan)
        make_widgets(organization, 50)

        service.check_subscription(subscription)

        assert notifier.calls == []

    @pytest.mark.parametrize("state", [BillingState.RESTRICTED, BillingState.CANCELLED])
    def test_restricted_and_cancelled_subscriptions_are_skipped(
        self, service, notifier, organization, subscription, membership, state
    ):
        """A restricted organization already knows it is blocked; a cancelled one
        is running out the clock, not accruing toward anything."""
        make_widgets(organization, 3)
        subscription.billing_state = state
        subscription.save(update_fields=["billing_state"])

        service.check_subscription(subscription)

        assert notifier.calls == []

    def test_one_resource_failing_does_not_stop_the_others(
        self, notifier, organization, subscription, membership, monkeypatch
    ):
        """The sweep is best-effort per resource: a failure on one must not cost
        the organization its warnings on every other."""
        service = UsageWarningService(notification_service=notifier)
        original = service.entitlement_service.get_effective_limit

        def explode_on_seats(org, resource_key, *args, **kwargs):
            if resource_key == "seats":
                raise RuntimeError("boom")
            return original(org, resource_key, *args, **kwargs)

        monkeypatch.setattr(service.entitlement_service, "get_effective_limit", explode_on_seats)
        make_widgets(organization, 3)

        service.check_subscription(subscription)

        assert len(notifier.calls) == 1

    def test_the_notification_carries_the_usage_and_the_ceiling(
        self, service, notifier, organization, subscription, membership
    ):
        make_widgets(organization, 3)

        service.check_subscription(subscription)

        context = notifier.calls[0]["context_kwargs"]
        assert context["current_usage"] == 3
        assert context["limit_value"] == 3

    def test_it_goes_to_the_configured_recipients(
        self, service, notifier, organization, subscription, user, membership
    ):
        make_widgets(organization, 3)

        service.check_subscription(subscription)

        assert notifier.calls[0]["user_id"] == user.pk

    def test_no_recipients_means_no_send_but_still_a_marker(
        self, service, notifier, organization, subscription
    ):
        """Nobody is a member, so there is nobody to tell -- but the threshold
        was still crossed, and re-checking every tick should not re-run."""
        make_widgets(organization, 3)

        service.check_subscription(subscription)

        assert notifier.calls == []
        assert LimitWarningNotification.objects.filter(resource_key="widgets").exists()


class TestDefaultNotifier:
    @override_settings(VINTA_BILLING={"METERED_RESOURCE_KEY": "event_occurrences"})
    def test_the_service_falls_back_to_the_configured_notifier(self):
        """Constructed bare, it must still have somewhere to send."""
        from billing.notifications import LoggingNotifier

        assert isinstance(UsageWarningService().notification_service, LoggingNotifier)
