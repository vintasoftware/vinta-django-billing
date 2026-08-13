"""The limit engine, end to end, against resources it has never heard of.

This is the test that matters most for the extraction: the engine resolves
ceilings, counts usage and refuses over-limit creates for ``widgets`` and
``seats`` -- two resources defined entirely in ``tests.testapp``, with counters
this package never sees.
"""

import pytest

from vinta_billing.models import PlanLimit, SubscriptionPlanLimit
from tests.testapp.billing_resources import EXCLUDE_INVITATION_ID
from tests.testapp.models import Seat, SeatInvitation, Widget


pytestmark = pytest.mark.django_db


class TestEffectiveLimit:
    def test_reads_the_ceiling_off_the_subscription(
        self, entitlement_service, organization, subscription
    ):
        limit = entitlement_service.get_effective_limit(organization, "widgets")

        assert limit.limit_value == 3
        assert limit.is_unlimited is False

    def test_a_null_limit_is_unlimited_not_zero(
        self, entitlement_service, organization, unlimited_plan, make_subscription
    ):
        """Treating NULL as zero would turn a data gap into a total lockout."""
        make_subscription(organization, unlimited_plan)

        limit = entitlement_service.get_effective_limit(organization, "widgets")

        assert limit.is_unlimited is True

    def test_a_missing_limit_row_is_unlimited(
        self, entitlement_service, organization, subscription
    ):
        """Absence must fail open, exactly like a NULL value.

        A resource registered after a plan was seeded has no row on that plan.
        Reading that as zero would lock every customer out of the new feature
        the moment it shipped.
        """
        SubscriptionPlanLimit.objects.filter(
            subscription=subscription, resource_key="widgets"
        ).delete()

        assert entitlement_service.get_effective_limit(organization, "widgets").is_unlimited

    def test_no_subscription_is_unlimited(self, entitlement_service, organization):
        assert entitlement_service.get_effective_limit(organization, "widgets").is_unlimited


class TestUsage:
    def test_counts_through_the_registered_counter(
        self, entitlement_service, organization, subscription
    ):
        Widget.objects.create(organization=organization, name="a")
        Widget.objects.create(organization=organization, name="b")

        assert entitlement_service.get_current_usage(organization, "widgets") == 2

    def test_usage_is_isolated_per_organization(
        self, entitlement_service, organization, other_organization, subscription
    ):
        Widget.objects.create(organization=other_organization, name="theirs")

        assert entitlement_service.get_current_usage(organization, "widgets") == 0

    def test_a_two_table_counter_sums_both_sides(
        self, entitlement_service, organization, subscription, user
    ):
        """`seats` counts real seats plus still-open invitations."""
        Seat.objects.create(organization=organization, user=user)
        SeatInvitation.objects.create(organization=organization, email="new@example.com")

        assert entitlement_service.get_current_usage(organization, "seats") == 2

    def test_extra_reaches_the_projects_counter(
        self, entitlement_service, organization, subscription, user
    ):
        """`usage_extra` is opaque to the engine and read only by the counter.

        This is what replaced the invitation-exclusion parameter the code was
        extracted with -- the engine no longer needs a concept of invitations.
        """
        Seat.objects.create(organization=organization, user=user)
        invitation = SeatInvitation.objects.create(
            organization=organization, email="new@example.com"
        )

        usage = entitlement_service.get_current_usage(
            organization, "seats", usage_extra={EXCLUDE_INVITATION_ID: invitation.pk}
        )

        assert usage == 1

    def test_an_unregistered_resource_reports_zero_rather_than_raising(
        self, entitlement_service, organization, subscription
    ):
        """Fails open: a stale plan row must not 500 an unrelated request."""
        assert entitlement_service.get_current_usage(organization, "never_registered") == 0


class TestCheckLimit:
    def test_allows_a_create_under_the_ceiling(
        self, entitlement_service, organization, subscription
    ):
        result = entitlement_service.check_limit(organization, "widgets", delta=1)

        assert result.allowed is True
        assert result.ceiling == 3

    def test_refuses_the_create_that_would_exceed_the_ceiling(
        self, entitlement_service, organization, subscription
    ):
        for name in ("a", "b", "c"):
            Widget.objects.create(organization=organization, name=name)

        result = entitlement_service.check_limit(organization, "widgets", delta=1)

        assert result.allowed is False
        assert result.current_usage == 3

    def test_allows_filling_the_last_unit_of_capacity(
        self, entitlement_service, organization, subscription
    ):
        """At usage 2 of 3, one more is exactly the ceiling and must be allowed."""
        Widget.objects.create(organization=organization, name="a")
        Widget.objects.create(organization=organization, name="b")

        assert entitlement_service.check_limit(organization, "widgets", delta=1).allowed is True

    def test_reports_the_registered_remedy_on_refusal(
        self, entitlement_service, organization, subscription, user
    ):
        """The remedy comes from the registration, so the client can route the
        user to the right screen."""
        SeatInvitation.objects.create(organization=organization, email="a@example.com")
        SeatInvitation.objects.create(organization=organization, email="b@example.com")

        result = entitlement_service.check_limit(organization, "seats", delta=1)

        assert result.allowed is False
        assert result.remedy == "purchase_add_on"

    def test_unlimited_never_counts_usage(
        self, entitlement_service, organization, unlimited_plan, make_subscription
    ):
        """The answer cannot depend on usage, so the count is skipped entirely."""
        make_subscription(organization, unlimited_plan)
        Widget.objects.create(organization=organization, name="a")

        result = entitlement_service.check_limit(organization, "widgets", delta=1000)

        assert result.allowed is True
        assert result.current_usage is None


class TestAddOns:
    def test_an_active_add_on_raises_the_ceiling(
        self, entitlement_service, organization, subscription
    ):
        from vinta_billing.models import SubscriptionAddOn

        SubscriptionAddOn.objects.create(
            subscription=subscription,
            resource_key="widgets",
            quantity=2,
            is_active=True,
            is_recurring=False,
        )

        assert entitlement_service.get_effective_limit(organization, "widgets").limit_value == 5

    def test_an_inactive_add_on_does_not(self, entitlement_service, organization, subscription):
        from vinta_billing.models import SubscriptionAddOn

        SubscriptionAddOn.objects.create(
            subscription=subscription,
            resource_key="widgets",
            quantity=2,
            is_active=False,
            is_recurring=False,
        )

        assert entitlement_service.get_effective_limit(organization, "widgets").limit_value == 3


class TestPlanLimitsAreCopiedOntoTheSubscription:
    def test_sync_copies_every_plan_row(self, subscription, plan):
        copied = set(
            SubscriptionPlanLimit.objects.filter(subscription=subscription).values_list(
                "resource_key", flat=True
            )
        )
        expected = set(PlanLimit.objects.filter(plan=plan).values_list("resource_key", flat=True))

        assert copied == expected
