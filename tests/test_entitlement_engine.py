"""The limit engine, end to end, against resources it has never heard of.

This is the test that matters most for the extraction: the engine resolves
ceilings, counts usage and refuses over-limit creates for ``widgets`` and
``seats`` -- two resources defined entirely in ``tests.testapp``, with counters
this package never sees.
"""

import pytest

from tests.testapp.billing_resources import EXCLUDE_INVITATION_ID
from tests.testapp.models import Seat, SeatInvitation, Widget
from vinta_billing.exceptions import InapplicableUsageExtraError
from vinta_billing.models import PlanLimit, SubscriptionPlanLimit


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


class TestLazyUsageExtra:
    """``usage_extra_resolver``: the lazy twin of ``usage_extra``.

    The case it exists for is accepting an invitation. Working out *which*
    invitation to exclude from the seat count is itself a query, and on the
    unlimited path the count it feeds is never taken -- so paying for that query
    is paying for an answer nobody reads. Every organization is unlimited for the
    length of a rollout, which is precisely when the cost is paid on every
    request and buys nothing.

    ``check_postpaid_allowance``'s ``delta_resolver`` already worked this way;
    this is the same idea on the prepaid side.
    """

    def test_the_resolved_extra_reaches_the_counter(
        self, entitlement_service, organization, subscription, user
    ):
        Seat.objects.create(organization=organization, user=user)
        invitation = SeatInvitation.objects.create(
            organization=organization, email="new@example.com"
        )

        result = entitlement_service.check_limit(
            organization,
            "seats",
            delta=1,
            usage_extra_resolver=lambda: {EXCLUDE_INVITATION_ID: invitation.pk},
        )

        # One seat and one invitation against a ceiling of 2. Without the
        # exclusion the accept fails its own check at exactly the ceiling.
        assert result.current_usage == 1
        assert result.allowed is True

    def test_it_matches_the_eager_argument_exactly(
        self, entitlement_service, organization, subscription, user
    ):
        Seat.objects.create(organization=organization, user=user)
        invitation = SeatInvitation.objects.create(
            organization=organization, email="new@example.com"
        )
        extra = {EXCLUDE_INVITATION_ID: invitation.pk}

        eager = entitlement_service.check_limit(organization, "seats", usage_extra=extra)
        lazy = entitlement_service.check_limit(
            organization, "seats", usage_extra_resolver=lambda: extra
        )

        assert eager == lazy

    def test_it_is_never_called_on_the_unlimited_path(
        self, entitlement_service, organization, unlimited_plan, make_subscription
    ):
        """The whole point. An unlimited ceiling skips the count, so there is
        nothing to hand the resolver's result to -- and the query it would run is
        pure cost on the hottest guarded path in a rolling-out product."""
        make_subscription(organization, unlimited_plan)
        calls = []

        result = entitlement_service.check_limit(
            organization,
            "seats",
            usage_extra_resolver=lambda: calls.append(1) or {},
        )

        assert result.allowed is True
        assert result.current_usage is None
        assert calls == []

    def test_it_is_called_exactly_once(self, entitlement_service, organization, subscription, user):
        """``_count_usage`` is a sum over ``_usage_breakdown`` and the remedy
        lookup reads the subscription again; neither may re-resolve."""
        Seat.objects.create(organization=organization, user=user)
        calls = []

        def resolver():
            calls.append(1)
            return {}

        entitlement_service.check_limit(organization, "seats", usage_extra_resolver=resolver)

        assert calls == [1]

    def test_it_is_still_called_when_the_check_refuses(
        self, entitlement_service, organization, subscription, user
    ):
        """A refusal reports ``current_usage``, so the count -- and the extra it
        depends on -- has to have happened."""
        SeatInvitation.objects.create(organization=organization, email="a@example.com")
        SeatInvitation.objects.create(organization=organization, email="b@example.com")
        calls = []

        result = entitlement_service.check_limit(
            organization, "seats", usage_extra_resolver=lambda: calls.append(1) or {}
        )

        assert result.allowed is False
        assert calls == [1]

    def test_it_is_not_called_for_a_restricted_billing_root(
        self, entitlement_service, organization, plan, make_subscription
    ):
        """RESTRICTED refuses every write before a ceiling is resolved at all, so
        there is no count for the resolver to feed there either."""
        from vinta_billing.constants import BillingState

        make_subscription(organization, plan, billing_state=BillingState.RESTRICTED)
        calls = []

        result = entitlement_service.check_limit(
            organization, "seats", usage_extra_resolver=lambda: calls.append(1) or {}
        )

        assert result.allowed is False
        assert calls == []

    def test_passing_both_raises(self, entitlement_service, organization, subscription):
        """Two sources for one field means one of them is silently dropped."""
        with pytest.raises(ValueError, match="not both"):
            entitlement_service.check_limit(
                organization,
                "seats",
                usage_extra={EXCLUDE_INVITATION_ID: 1},
                usage_extra_resolver=lambda: {EXCLUDE_INVITATION_ID: 2},
            )

    def test_neither_leaves_the_call_unchanged(
        self, entitlement_service, organization, subscription, user
    ):
        """The signature a 0.3.0 caller wrote still means what it meant."""
        Seat.objects.create(organization=organization, user=user)
        SeatInvitation.objects.create(organization=organization, email="new@example.com")

        result = entitlement_service.check_limit(organization, "seats", delta=1)

        assert result.current_usage == 2
        assert result.allowed is False


class TestInapplicableUsageExtra:
    """A per-call extra is read by exactly one counter, and ignored by every
    other. Aiming one at the wrong resource is a no-op that *looks* like it
    worked: the caller gets a count computed as though nothing had been passed,
    and nothing in the answer says so.

    ``tests.testapp`` declares ``seats`` as reading ``exclude_invitation_id``,
    declares ``event_occurrences`` as reading nothing at all, and deliberately
    leaves ``widgets`` undeclared -- the shape of every registration written
    before 0.4.0.
    """

    def test_a_key_the_resource_does_not_read_is_refused(
        self, entitlement_service, organization, subscription
    ):
        with pytest.raises(InapplicableUsageExtraError) as excinfo:
            entitlement_service.check_limit(
                organization, "event_occurrences", usage_extra={EXCLUDE_INVITATION_ID: 1}
            )

        assert excinfo.value.resource_key == "event_occurrences"
        assert excinfo.value.unexpected_keys == [EXCLUDE_INVITATION_ID]

    def test_a_declared_key_is_allowed(self, entitlement_service, organization, subscription, user):
        invitation = SeatInvitation.objects.create(
            organization=organization, email="new@example.com"
        )

        result = entitlement_service.check_limit(
            organization, "seats", usage_extra={EXCLUDE_INVITATION_ID: invitation.pk}
        )

        assert result.current_usage == 0

    def test_a_typo_on_a_declared_resource_is_refused(
        self, entitlement_service, organization, subscription
    ):
        """The failure this is really for: the key is right for this resource but
        misspelled, so the counter silently excludes nothing."""
        with pytest.raises(InapplicableUsageExtraError):
            entitlement_service.check_limit(
                organization, "seats", usage_extra={"exclude_invite_id": 1}
            )

    def test_an_undeclared_resource_still_takes_anything(
        self, entitlement_service, organization, subscription
    ):
        """Backwards compatibility, and the reason ``usage_extra_keys`` defaults
        to ``None`` rather than an empty set: a 0.3.0 registration declares
        nothing and must keep forwarding ``usage_extra`` opaquely."""
        result = entitlement_service.check_limit(
            organization, "widgets", usage_extra={"anything at all": 1}
        )

        assert result.allowed is True

    def test_it_is_refused_on_the_unlimited_path_too(
        self, entitlement_service, organization, unlimited_plan, make_subscription
    ):
        """Checked before the unlimited short-circuit, not alongside the count.
        Every organization is unlimited during a rollout -- exactly when a call
        site is new and most likely to be wrong -- so deferring the check to the
        counting branch would report nothing to anyone."""
        make_subscription(organization, unlimited_plan)

        with pytest.raises(InapplicableUsageExtraError):
            entitlement_service.check_limit(
                organization, "seats", usage_extra={"exclude_invite_id": 1}
            )

    def test_a_resolvers_result_is_checked_the_same_way(
        self, entitlement_service, organization, subscription
    ):
        with pytest.raises(InapplicableUsageExtraError):
            entitlement_service.check_limit(
                organization, "seats", usage_extra_resolver=lambda: {"exclude_invite_id": 1}
            )

    def test_the_read_methods_are_held_to_the_same_rule(
        self, entitlement_service, organization, subscription
    ):
        with pytest.raises(InapplicableUsageExtraError):
            entitlement_service.get_current_usage(
                organization, "seats", usage_extra={"exclude_invite_id": 1}
            )
        with pytest.raises(InapplicableUsageExtraError):
            entitlement_service.get_usage_breakdown(
                organization, "seats", usage_extra={"exclude_invite_id": 1}
            )

    def test_it_is_not_a_valueerror(self):
        """``except ValueError`` wrappers around service calls are common, and
        would flatten a call-site bug into a user-facing validation message."""
        assert not issubclass(InapplicableUsageExtraError, ValueError)
