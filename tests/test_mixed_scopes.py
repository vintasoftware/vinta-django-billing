"""One project, selling a personal plan and a team plan at once.

This is the claim the 0.8 change exists to make, and the rest of the suite does
not make it: every other settings module bills exactly one kind of payer, so
"billing works" there says nothing about whether two kinds can coexist without
leaking into each other.

Runs only under ``tests.settings_mixed``, where ``BILLING_SCOPE_MODEL`` names
``swapped_scopes.MixedScope`` -- a nullable ``user``, a nullable ``company``,
and a CHECK constraint saying a scope is exactly one of the two.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from tests.conftest import make_scope
from tests.testapp.models import Company, Widget
from vinta_billing.conf import get_scope_model, scope_model_string
from vinta_billing.constants import BillingInterval, BillingState, LimitKind, ScopeType
from vinta_billing.models import BillingPlan, PlanLimit, Subscription


pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        scope_model_string() != "swapped_scopes.MixedScope",
        reason="needs the mixed scope model; run tox -e mixed",
    ),
]


@pytest.fixture
def solo(db):
    """A person on a personal plan."""
    return get_user_model().objects.create_user(username="solo", password="pw")


@pytest.fixture
def team(db):
    """A company on a team plan."""
    return Company.objects.create(name="Initech")


@pytest.fixture
def small_plan(db):
    plan = BillingPlan.objects.create(
        name="Solo", slug="solo", monthly_price=Decimal("5.00"), annual_price=Decimal("50.00")
    )
    PlanLimit.objects.create(
        plan=plan, resource_key="widgets", limit_value=2, kind=LimitKind.PREPAID
    )
    return plan


@pytest.fixture
def big_plan(db):
    plan = BillingPlan.objects.create(
        name="Team", slug="team", monthly_price=Decimal("50.00"), annual_price=Decimal("500.00")
    )
    PlanLimit.objects.create(
        plan=plan, resource_key="widgets", limit_value=10, kind=LimitKind.PREPAID
    )
    return plan


def _subscribe(scope, plan):
    now = timezone.now()
    subscription = Subscription.objects.create(
        scope=scope,
        plan=plan,
        billing_state=BillingState.ACTIVE,
        billing_interval=BillingInterval.MONTHLY,
        current_period_start=now - datetime.timedelta(days=1),
        current_period_end=now + datetime.timedelta(days=29),
    )
    from vinta_billing.services.container import get_subscription_service

    get_subscription_service()._sync_limits(subscription, plan)
    return subscription


class TestTwoKindsOfPayerCoexist:
    def test_a_user_and_a_company_each_hold_their_own_subscription(
        self, solo, team, small_plan, big_plan
    ):
        personal = make_scope(solo)
        corporate = make_scope(team)

        personal_subscription = _subscribe(personal, small_plan)
        team_subscription = _subscribe(corporate, big_plan)

        assert personal.scope_type == ScopeType.USER
        assert corporate.scope_type == ScopeType.ORGANIZATION
        assert personal_subscription.plan_id == small_plan.pk
        assert team_subscription.plan_id == big_plan.pk
        # Distinct rows, distinct keys, one table.
        assert personal.pk != corporate.pk
        assert {personal.scope_key, corporate.scope_key} == {
            "user:%d" % solo.pk,
            "company:%d" % team.pk,
        }

    def test_the_personal_scope_is_owned_by_the_person(self, solo):
        """Which is what makes the shipped permission default work unconfigured."""
        from vinta_billing.permissions import owner_may_manage_billing

        personal = make_scope(solo)

        assert personal.owner_id == solo.pk
        assert owner_may_manage_billing(solo, personal) is True

    def test_a_company_scope_is_not_manageable_by_an_unrelated_person(self, solo, team):
        from vinta_billing.permissions import owner_may_manage_billing

        corporate = make_scope(team)

        assert owner_may_manage_billing(solo, corporate) is False


class TestUsageDoesNotCrossBetweenThem:
    def test_each_ceiling_counts_only_its_own_rows(self, solo, team, small_plan, big_plan):
        """The failure this guards against is a billing one, not a crash.

        If the counters grouped by anything coarser than the scope, the person's
        two widgets would count against the company's ceiling and vice versa --
        and the first anyone would know is a wrong invoice.
        """
        personal = make_scope(solo)
        corporate = make_scope(team)
        _subscribe(personal, small_plan)
        _subscribe(corporate, big_plan)

        for index in range(2):
            Widget.objects.create(scope=personal, name="p%d" % index)
        for index in range(7):
            Widget.objects.create(scope=corporate, name="t%d" % index)

        from vinta_billing.services.container import get_entitlement_service

        service = get_entitlement_service()
        assert service.get_current_usage(personal, "widgets") == 2
        assert service.get_current_usage(corporate, "widgets") == 7

    def test_the_person_hits_their_own_small_ceiling(self, solo, team, small_plan, big_plan):
        """And the company's roomier plan does not rescue them."""
        personal = make_scope(solo)
        corporate = make_scope(team)
        _subscribe(personal, small_plan)
        _subscribe(corporate, big_plan)

        for index in range(2):
            Widget.objects.create(scope=personal, name="p%d" % index)

        from vinta_billing.services.container import get_entitlement_service

        service = get_entitlement_service()

        # At the ceiling: 2 of 2 used, so the next one is refused -- while the
        # company, on the roomier plan, is nowhere near its own.
        assert service.check_limit(personal, "widgets", delta=1).allowed is False
        assert service.check_limit(corporate, "widgets", delta=1).allowed is True


class TestTheDatabaseHoldsTheInvariant:
    def test_a_scope_naming_both_payers_is_refused(self, solo, team):
        """``save`` is bypassed by ``bulk_create``; the CHECK constraint is not."""
        model = get_scope_model()

        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.bulk_create(
                [model(scope_type=ScopeType.USER, scope_key="both", user=solo, company=team)]
            )

    def test_a_scope_naming_neither_is_refused(self):
        model = get_scope_model()

        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.bulk_create([model(scope_type=ScopeType.USER, scope_key="neither")])


class TestHierarchyAcrossKinds:
    def test_a_personal_scope_can_bill_against_a_company_root(self, solo, team, big_plan):
        """A consultant under an agency, say -- the shape a single-payer model
        cannot express at all."""
        from vinta_billing.hierarchy import ParentFieldHierarchy

        root = make_scope(team)
        child = make_scope(solo, parent=root)

        hierarchy = ParentFieldHierarchy()
        assert hierarchy.is_billing_root(root) is True
        assert hierarchy.is_billing_root(child) is False
        assert hierarchy.resolve_billing_root(child) == root
        assert set(hierarchy.pooled_scope_ids(root)) == {root.pk, child.pk}
