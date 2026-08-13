"""Provisioning a subscription, and moving it between plans.

The plan-change path is where a downgrade can silently become an upgrade: limits
and entitlements fail open and closed respectively, so pruning one like the other
grants an infinite ceiling. These tests pin the asymmetry.
"""

from decimal import Decimal

import pytest
from django.test import override_settings

from vinta_billing.constants import BillingInterval, BillingState, LimitKind
from vinta_billing.exceptions import IncompleteBillingPlanError, NoDefaultBillingPlanError
from vinta_billing.models import (
    BillingPlan,
    PlanEntitlement,
    PlanLimit,
    Subscription,
    SubscriptionAddOn,
    SubscriptionEntitlement,
    SubscriptionPlanLimit,
)
from vinta_billing.services.container import get_subscription_service


pytestmark = pytest.mark.django_db


@pytest.fixture
def service():
    return get_subscription_service()


def make_plan(slug, *, default=False, limits=None, entitlements=None):
    """A complete plan: every registered resource gets a row, as the engine requires."""
    plan = BillingPlan.objects.create(
        name=slug.title(),
        slug=slug,
        monthly_price=Decimal("10.00"),
        annual_price=Decimal("100.00"),
        is_active=True,
        is_default_for_new_organizations=default,
    )
    limits = limits or {}
    for key, kind in (
        ("widgets", LimitKind.PREPAID),
        ("seats", LimitKind.PREPAID),
        ("event_occurrences", LimitKind.POSTPAID),
    ):
        PlanLimit.objects.create(
            plan=plan, resource_key=key, limit_value=limits.get(key), kind=kind
        )
    for key, enabled in (entitlements or {}).items():
        PlanEntitlement.objects.create(plan=plan, entitlement_key=key, is_enabled=enabled)
    return plan


class TestCreateSubscription:
    def test_creates_the_subscription_with_its_limit_rows(self, service, organization):
        plan = make_plan("starter-a", default=True, limits={"widgets": 5})

        subscription = service.create_subscription_for_organization(organization)

        assert subscription is not None
        assert subscription.plan == plan
        assert subscription.billing_state == BillingState.FREE
        assert set(subscription.limits.values_list("resource_key", flat=True)) == {
            "widgets",
            "seats",
            "event_occurrences",
        }

    def test_copies_entitlement_rows_too(self, service, organization):
        make_plan("starter-b", default=True, entitlements={"white_label": True})

        subscription = service.create_subscription_for_organization(organization)

        assert subscription.entitlements.get(entitlement_key="white_label").is_enabled is True

    def test_is_idempotent(self, service, organization):
        """Two requests racing to provision the same organization must not both
        create a row -- `Subscription.organization` is a OneToOneField."""
        make_plan("starter-c", default=True)

        first = service.create_subscription_for_organization(organization)
        second = service.create_subscription_for_organization(organization)

        assert first.pk == second.pk
        assert Subscription.objects.filter(organization=organization).count() == 1

    def test_backfills_limits_onto_a_subscription_that_has_none(
        self, service, organization, plan, make_subscription
    ):
        """A row created by hand (admin, or a direct provider call) is otherwise
        returned silently with no limits to enforce."""
        subscription = make_subscription(organization, plan, sync_limits=False)
        assert not subscription.limits.exists()

        service.create_subscription_for_organization(organization, plan)

        assert subscription.limits.exists()

    def test_an_explicit_plan_overrides_the_default(self, service, organization):
        make_plan("default-d", default=True)
        chosen = make_plan("chosen-d")

        subscription = service.create_subscription_for_organization(organization, chosen)

        assert subscription.plan == chosen

    def test_no_default_plan_raises_rather_than_500ing(self, service, organization):
        """A deactivated default plan must not take down every signup."""
        with pytest.raises(NoDefaultBillingPlanError):
            service.create_subscription_for_organization(organization)

    def test_an_inactive_default_plan_does_not_count(self, service, organization):
        plan = make_plan("inactive-e", default=True)
        plan.is_active = False
        plan.save(update_fields=["is_active"])

        with pytest.raises(NoDefaultBillingPlanError):
            service.create_subscription_for_organization(organization)

    def test_an_incomplete_plan_is_refused(self, service, organization, db):
        incomplete = BillingPlan.objects.create(
            name="Incomplete",
            slug="incomplete-f",
            monthly_price=Decimal("1.00"),
            annual_price=Decimal("10.00"),
            is_active=True,
        )

        with pytest.raises(IncompleteBillingPlanError):
            service.create_subscription_for_organization(organization, incomplete)

    def test_stamps_the_resolved_provider(self, service, organization):
        make_plan("starter-g", default=True)

        with override_settings(VINTA_BILLING={"DEFAULT_PROVIDER": "stripe"}):
            subscription = service.create_subscription_for_organization(organization)

        assert subscription.payment_provider == "stripe"

    def test_an_unconfigured_provider_resolves_to_none_rather_than_raising(
        self, service, organization
    ):
        """A project billing nothing yet should not have to name a provider."""
        make_plan("starter-h", default=True)

        subscription = service.create_subscription_for_organization(organization)

        assert subscription.payment_provider == ""


class TestChangePlan:
    def test_refreshes_limits_from_the_new_plan(self, service, organization, make_subscription):
        old = make_plan("old-i", limits={"widgets": 1})
        new = make_plan("new-i", limits={"widgets": 9})
        subscription = make_subscription(organization, old)

        service.change_plan(subscription, new)

        assert subscription.limits.get(resource_key="widgets").limit_value == 9

    def test_leaves_an_overridden_row_alone(self, service, organization, make_subscription):
        """The support lever for a stuck organization must survive a plan change."""
        old = make_plan("old-j", limits={"widgets": 1})
        new = make_plan("new-j", limits={"widgets": 9})
        subscription = make_subscription(organization, old)
        subscription.limits.filter(resource_key="widgets").update(
            limit_value=99, is_overridden=True
        )

        service.change_plan(subscription, new)

        assert subscription.limits.get(resource_key="widgets").limit_value == 99

    def test_revokes_an_entitlement_the_new_plan_omits(
        self, service, organization, make_subscription
    ):
        """Entitlements fail *closed*, so dropping the row is what a downgrade
        means."""
        old = make_plan("old-k", entitlements={"white_label": True})
        new = make_plan("new-k")
        subscription = make_subscription(organization, old)
        service._sync_entitlements(subscription, old)
        assert subscription.entitlements.filter(entitlement_key="white_label").exists()

        service.change_plan(subscription, new)

        assert not subscription.entitlements.filter(entitlement_key="white_label").exists()

    def test_keeps_an_overridden_entitlement(self, service, organization, make_subscription):
        old = make_plan("old-l", entitlements={"white_label": True})
        new = make_plan("new-l")
        subscription = make_subscription(organization, old)
        service._sync_entitlements(subscription, old)
        subscription.entitlements.filter(entitlement_key="white_label").update(is_overridden=True)

        service.change_plan(subscription, new)

        assert subscription.entitlements.get(entitlement_key="white_label").is_enabled is True

    def test_prunes_a_row_for_a_retired_resource_key(
        self, service, organization, make_subscription
    ):
        """A key that left the registry can never be consulted again.

        Safe only because an incomplete plan is refused up front -- otherwise
        this would compose with the fail-open-on-absence rule into "downgrading
        to a plan that omits a resource grants it an infinite ceiling".
        """
        plan = make_plan("plan-m")
        subscription = make_subscription(organization, plan)
        SubscriptionPlanLimit.objects.create(
            subscription=subscription,
            resource_key="retired_key",
            limit_value=3,
            kind=LimitKind.PREPAID,
        )

        service.change_plan(subscription, plan)

        assert not subscription.limits.filter(resource_key="retired_key").exists()

    def test_does_not_prune_an_overridden_retired_row(
        self, service, organization, make_subscription
    ):
        plan = make_plan("plan-n")
        subscription = make_subscription(organization, plan)
        SubscriptionPlanLimit.objects.create(
            subscription=subscription,
            resource_key="retired_key",
            limit_value=3,
            kind=LimitKind.PREPAID,
            is_overridden=True,
        )

        service.change_plan(subscription, plan)

        assert subscription.limits.filter(resource_key="retired_key").exists()

    def test_moving_to_an_incomplete_plan_is_refused(
        self, service, organization, make_subscription, db
    ):
        plan = make_plan("plan-o")
        subscription = make_subscription(organization, plan)
        incomplete = BillingPlan.objects.create(
            name="Incomplete",
            slug="incomplete-o",
            monthly_price=Decimal("1.00"),
            annual_price=Decimal("10.00"),
            is_active=True,
        )

        with pytest.raises(IncompleteBillingPlanError):
            service.change_plan(subscription, incomplete)

    def test_a_refused_change_leaves_the_subscription_on_its_old_plan(
        self, service, organization, make_subscription, db
    ):
        plan = make_plan("plan-p")
        subscription = make_subscription(organization, plan)
        incomplete = BillingPlan.objects.create(
            name="Incomplete",
            slug="incomplete-p",
            monthly_price=Decimal("1.00"),
            annual_price=Decimal("10.00"),
            is_active=True,
        )

        with pytest.raises(IncompleteBillingPlanError):
            service.change_plan(subscription, incomplete)

        subscription.refresh_from_db()
        assert subscription.plan == plan


class TestSyncEntitlements:
    def test_updates_a_changed_flag(self, service, organization, make_subscription):
        plan = make_plan("plan-q", entitlements={"white_label": True})
        subscription = make_subscription(organization, plan)
        service._sync_entitlements(subscription, plan)

        plan.entitlements.filter(entitlement_key="white_label").update(is_enabled=False)
        service._sync_entitlements(subscription, plan)

        assert subscription.entitlements.get(entitlement_key="white_label").is_enabled is False

    def test_is_idempotent(self, service, organization, make_subscription):
        plan = make_plan("plan-r", entitlements={"white_label": True})
        subscription = make_subscription(organization, plan)

        service._sync_entitlements(subscription, plan)
        service._sync_entitlements(subscription, plan)

        assert (
            SubscriptionEntitlement.objects.filter(
                subscription=subscription, entitlement_key="white_label"
            ).count()
            == 1
        )


class TestAddOns:
    def test_activating_grants_capacity(self, subscription):
        add_on = SubscriptionAddOn.objects.create(
            subscription=subscription,
            resource_key="widgets",
            quantity=2,
            is_active=False,
            is_recurring=False,
        )

        get_subscription_service().activate_add_on(add_on)

        add_on.refresh_from_db()
        assert add_on.is_active is True

    def test_activating_twice_cannot_double_grant(self, subscription):
        """A provider redelivery must not add capacity a second time."""
        add_on = SubscriptionAddOn.objects.create(
            subscription=subscription,
            resource_key="widgets",
            quantity=2,
            is_active=True,
            is_recurring=False,
        )
        service = get_subscription_service()

        service.activate_add_on(add_on)
        service.activate_add_on(add_on)

        assert SubscriptionAddOn.objects.filter(subscription=subscription).count() == 1
        add_on.refresh_from_db()
        assert add_on.quantity == 2

    def test_cancelling_stops_renewal_but_keeps_this_periods_capacity(self, subscription):
        """Capacity already paid for must stay in effect until the boundary."""
        add_on = SubscriptionAddOn.objects.create(
            subscription=subscription,
            resource_key="widgets",
            quantity=2,
            is_active=True,
            is_recurring=True,
        )

        get_subscription_service().cancel_add_on(add_on)

        add_on.refresh_from_db()
        assert add_on.is_recurring is False
        assert add_on.is_active is True

    def test_cancelling_a_one_off_add_on_is_a_no_op(self, subscription):
        add_on = SubscriptionAddOn.objects.create(
            subscription=subscription,
            resource_key="widgets",
            quantity=2,
            is_active=True,
            is_recurring=False,
        )

        get_subscription_service().cancel_add_on(add_on)

        add_on.refresh_from_db()
        assert add_on.is_active is True


class TestBillingIntervalOnCreate:
    def test_a_new_subscription_starts_monthly(self, service, organization):
        """The stored period is monthly for every plan, because overage settles
        monthly regardless of how the fee is billed."""
        make_plan("starter-s", default=True)

        subscription = service.create_subscription_for_organization(organization)

        assert subscription.billing_interval == BillingInterval.MONTHLY
