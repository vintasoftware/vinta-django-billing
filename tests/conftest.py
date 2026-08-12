"""Fixtures shared across the suite.

Everything here builds against a stock ``vinta-django-orgs`` install and the
resources ``tests.testapp`` registers. Nothing reaches for a field the
organization model does not have -- that is most of what the suite is proving.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from organizations.conf import get_organization_membership_model, get_organization_model

from billing.constants import BillingInterval, BillingState, LimitKind
from billing.models import BillingPlan, PlanLimit, Subscription
from billing.services.container import reset_services


@pytest.fixture(autouse=True)
def _reset_service_cache():
    """Drop the cached service graph around every test.

    The factories in ``billing.services.container`` are ``lru_cache``d, so a
    test that reconfigures ``VINTA_BILLING`` would otherwise get a service built
    against the previous settings.
    """
    reset_services()
    yield
    reset_services()


@pytest.fixture
def organization(db):
    return get_organization_model().objects.create(name="Acme", slug="acme")


@pytest.fixture
def other_organization(db):
    return get_organization_model().objects.create(name="Globex", slug="globex")


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="ada", password="pw")


@pytest.fixture
def membership(db, organization, user):
    return get_organization_membership_model().objects.create(organization=organization, user=user)


@pytest.fixture
def plan(db):
    """A plan with a real ceiling on both prepaid resources."""
    billing_plan = BillingPlan.objects.create(
        name="Starter",
        slug="starter",
        monthly_price=Decimal("10.00"),
        annual_price=Decimal("100.00"),
        is_active=True,
    )
    PlanLimit.objects.create(
        plan=billing_plan, resource_key="widgets", limit_value=3, kind=LimitKind.PREPAID
    )
    PlanLimit.objects.create(
        plan=billing_plan, resource_key="seats", limit_value=2, kind=LimitKind.PREPAID
    )
    PlanLimit.objects.create(
        plan=billing_plan,
        resource_key="event_occurrences",
        limit_value=100,
        kind=LimitKind.POSTPAID,
        overage_unit_price=Decimal("0.10"),
    )
    return billing_plan


@pytest.fixture
def unlimited_plan(db):
    """A plan whose limits are all NULL -- the "unlimited" shape."""
    billing_plan = BillingPlan.objects.create(
        name="Unlimited",
        slug="unlimited",
        monthly_price=Decimal("0.00"),
        annual_price=Decimal("0.00"),
        is_active=True,
    )
    for key in ("widgets", "seats"):
        PlanLimit.objects.create(
            plan=billing_plan, resource_key=key, limit_value=None, kind=LimitKind.PREPAID
        )
    return billing_plan


def _make_subscription(organization, plan, **kwargs):
    now = timezone.now()
    defaults = {
        "organization": organization,
        "plan": plan,
        "billing_state": BillingState.ACTIVE,
        "billing_interval": BillingInterval.MONTHLY,
        "current_period_start": now - datetime.timedelta(days=1),
        "current_period_end": now + datetime.timedelta(days=29),
    }
    defaults.update(kwargs)
    return Subscription.objects.create(**defaults)


@pytest.fixture
def subscription(db, organization, plan):
    """An active subscription, with its plan's limits copied onto it.

    Created through ``SubscriptionService`` rather than by hand where possible,
    so the per-subscription limit rows the engine actually reads exist.
    """
    from billing.services.container import get_subscription_service

    sub = _make_subscription(organization, plan)
    get_subscription_service()._sync_limits(sub, plan)
    return sub


@pytest.fixture
def make_subscription(db):
    """Build a subscription for an arbitrary organization/plan pair."""

    def factory(organization, plan, sync_limits=True, **kwargs):
        from billing.services.container import get_subscription_service

        sub = _make_subscription(organization, plan, **kwargs)
        if sync_limits:
            get_subscription_service()._sync_limits(sub, plan)
        return sub

    return factory


@pytest.fixture
def entitlement_service():
    from billing.services.container import get_entitlement_service

    return get_entitlement_service()
