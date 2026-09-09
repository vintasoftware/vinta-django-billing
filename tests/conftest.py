"""Fixtures shared across the suite.

Billing hangs off scopes, and a scope names some payer the *project* owns. The
suite keeps both in play: ``organization`` is the payer (a stock
``vinta-django-orgs`` organization, still what the permission and membership
tests need), and ``scope`` is the billing-side row that names it. Tests that
only bill ask for ``scope``; tests about who may manage billing ask for both.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from tests.payers import make_payer, payers_are_organizations
from vinta_billing.conf import get_scope_model
from vinta_billing.constants import BillingInterval, BillingState, LimitKind
from vinta_billing.models import BillingPlan, PlanLimit, Subscription
from vinta_billing.services.container import reset_services


def make_scope(payer, **kwargs):
    """The scope for ``payer``, under whatever ``BILLING_SCOPE_MODEL`` names.

    Goes through the configured model's own ``get_or_create_for`` rather than
    building rows by hand: the shipped model has a generic key, a swapped-in one
    may have typed foreign keys, and only the model knows how to name a payer.
    """
    return get_scope_model()._default_manager.get_or_create_for(payer, **kwargs)[0]


@pytest.fixture(autouse=True)
def _reset_service_cache():
    """Drop the cached service graph around every test.

    The factories in ``vinta_billing.services.container`` are ``lru_cache``d, so a
    test that reconfigures ``VINTA_BILLING`` would otherwise get a service built
    against the previous settings.
    """
    reset_services()
    yield
    reset_services()


@pytest.fixture
def organization(db):
    """The payer a scope names.

    Still called ``organization`` because that is what it is under the default
    settings and in most of the suite's prose. Under a settings module that
    swapped the scope model it is a ``testapp.Company`` instead -- see
    ``tests/payers.py``. Tests that need a real ``vinta-django-orgs``
    organization specifically say so with ``needs_organization_payers``.
    """
    return make_payer("Acme")


@pytest.fixture
def other_organization(db):
    return make_payer("Globex")


@pytest.fixture
def scope(db, organization, user):
    """The billing scope for ``organization``, owned by ``user``.

    An organization scope, deliberately: it is the shape every pre-scope
    installation had, so the bulk of the suite goes on exercising the same
    thing it always did. The user-scope path gets its own tests rather than
    being smuggled in as the default here.

    ``owner`` is set because the shipped permission and recipient defaults both
    read it -- it is what the membership row used to be for. A test that wants
    a scope nobody manages asks for ``unowned_scope``.
    """
    scope = make_scope(organization)
    scope.owner = user
    scope.save(update_fields=["owner", "modified"])
    return scope


@pytest.fixture
def other_scope(db, other_organization):
    """A second tenant, deliberately ownerless.

    Every "is another tenant's billing refused?" test leans on this, and an
    owner here would make some of them pass for the wrong reason.
    """
    return make_scope(other_organization)


@pytest.fixture
def unowned_scope(db, organization):
    """``organization``'s scope with no billing owner set."""
    return make_scope(organization)


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="ada", password="pw")


@pytest.fixture
def membership(db, organization, user):
    """A ``vinta-django-orgs`` membership.

    Only meaningful where the payer is an organization, so it skips rather than
    erroring under a settings module whose payers are something else -- the
    tests that ask for it are about ``vinta_billing.contrib.orgs``.
    """
    if not payers_are_organizations():
        pytest.skip("payers are not vinta-django-orgs organizations under these settings")
    from vinta_orgs.conf import get_organization_membership_model

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


def _make_subscription(scope, plan, **kwargs):
    now = timezone.now()
    defaults = {
        "scope": scope,
        "plan": plan,
        "billing_state": BillingState.ACTIVE,
        "billing_interval": BillingInterval.MONTHLY,
        "current_period_start": now - datetime.timedelta(days=1),
        "current_period_end": now + datetime.timedelta(days=29),
    }
    defaults.update(kwargs)
    return Subscription.objects.create(**defaults)


@pytest.fixture
def subscription(db, scope, plan):
    """An active subscription, with its plan's limits copied onto it.

    Created through ``SubscriptionService`` rather than by hand where possible,
    so the per-subscription limit rows the engine actually reads exist.
    """
    from vinta_billing.services.container import get_subscription_service

    sub = _make_subscription(scope, plan)
    get_subscription_service()._sync_limits(sub, plan)
    return sub


@pytest.fixture
def make_subscription(db):
    """Build a subscription for an arbitrary scope/plan pair."""

    def factory(scope, plan, sync_limits=True, **kwargs):
        from vinta_billing.services.container import get_subscription_service

        sub = _make_subscription(scope, plan, **kwargs)
        if sync_limits:
            get_subscription_service()._sync_limits(sub, plan)
        return sub

    return factory


@pytest.fixture
def entitlement_service():
    from vinta_billing.services.container import get_entitlement_service

    return get_entitlement_service()


@pytest.fixture
def billing_address(db):
    from vinta_billing.models import BillingAddress

    return BillingAddress.objects.create(
        street_name="Main Street",
        street_number="1",
        city="Springfield",
        state="SP",
        country="BR",
        zip_code="00000-000",
    )


@pytest.fixture
def billing_profile(db, scope, billing_address):
    """The payer record every charge hangs off."""
    from vinta_billing.constants import DocumentTypes
    from vinta_billing.models import BillingProfile

    return BillingProfile.objects.create(
        scope=scope,
        contact_first_name="Ada",
        contact_last_name="Lovelace",
        contact_email="billing@example.com",
        document_type=DocumentTypes.OTHER,
        document_number="1",
        billing_address=billing_address,
    )
