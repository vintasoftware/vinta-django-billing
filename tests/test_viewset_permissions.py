"""The object-level billing gate, exercised through the mounted viewsets.

``tests/test_request_seams.py`` tests ``IsBillingManager`` in isolation, by
calling ``has_object_permission`` with an object it built. That is exactly how
an authorization bypass survived two releases: every object-level check the
shipped viewsets make passes a **billing root**, which is a scope, and nothing
in the suite ever sent a request through one of those viewsets to find out what
the permission class did with it.

So the tests here send real requests, through the router the README tells a
project to mount, to the endpoints that check against a resolved billing root:

* ``GET  /billing/usage/occurrences/`` (``MeteredOccurrenceViewSet.list``)
* ``POST /billing/subscription/change-plan/`` and its two sibling write actions
  (``SubscriptionViewSet.get_subscription(check_object_perms=True)``)
* ``POST /billing/add-ons/`` (``AddOnViewSet.create``)

The caller in each is the owner of a *child* scope that bills against a reseller
root they do not own. The request-level check passes -- they do own something --
and the object-level check is the only thing standing between them and the
root's plan, the root's payment method and the root's usage.

The reseller shape is built with the stock ``ParentFieldHierarchy`` over
``BillingScope.parent``. Before scopes this file had to define a hierarchy class
of its own, because the organization model had no parent for the engine to walk.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from vinta_billing.constants import BillingInterval, BillingState, LimitKind
from vinta_billing.models import BillingPlan, BillingScope, PlanLimit, Subscription


pytestmark = pytest.mark.django_db


def resolve_scope_by_owner(request):
    """``SCOPE_RESOLVER`` for this module: the scope the caller owns.

    Stands in for whatever a project uses to decide which tenant a request acts
    on. Deliberately generous -- it resolves *a* scope for any authenticated
    caller -- so that the request-level check always passes and the object-level
    check is the only thing these tests can be measuring.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    return BillingScope.objects.filter(owner=user).first()


reseller_settings = override_settings(
    VINTA_BILLING={
        "HIERARCHY": "vinta_billing.hierarchy.ParentFieldHierarchy",
        "SCOPE_RESOLVER": "tests.test_viewset_permissions.resolve_scope_by_owner",
    }
)


@pytest.fixture(autouse=True)
def _reseller_hierarchy():
    with reseller_settings:
        yield


@pytest.fixture
def reseller_root(db):
    """The paying root. Parentless, so ``ParentFieldHierarchy`` calls it a root."""
    owner = get_user_model().objects.create_user(username="root-owner", password="pw")
    return BillingScope.objects.create(
        scope_type="organization",
        scope_key="reseller-root",
        label="Reseller",
        owner=owner,
        content_type=_user_content_type(),
        object_id=str(owner.pk),
    )


@pytest.fixture
def child(db, reseller_root):
    """A scope that bills against ``reseller_root`` and owns only itself."""
    owner = get_user_model().objects.create_user(username="child-owner", password="pw")
    return BillingScope.objects.create(
        scope_type="organization",
        scope_key="child",
        label="Child",
        owner=owner,
        parent=reseller_root,
        content_type=_user_content_type(),
        object_id=str(owner.pk),
    )


def _user_content_type():
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(get_user_model())


@pytest.fixture
def root_plan(db):
    plan = BillingPlan.objects.create(
        name="Root Plan",
        slug="root-plan",
        monthly_price=Decimal("10.00"),
        annual_price=Decimal("100.00"),
        is_active=True,
    )
    PlanLimit.objects.create(
        plan=plan,
        resource_key="widgets",
        limit_value=3,
        kind=LimitKind.PREPAID,
        overage_unit_price=Decimal("1.00"),
    )
    return plan


@pytest.fixture
def root_subscription(db, reseller_root, root_plan):
    """The subscription the child scope must not be able to touch."""
    now = timezone.now()
    subscription = Subscription.objects.create(
        scope=reseller_root,
        plan=root_plan,
        billing_state=BillingState.ACTIVE,
        billing_interval=BillingInterval.MONTHLY,
        current_period_start=now - datetime.timedelta(days=1),
        current_period_end=now + datetime.timedelta(days=29),
    )
    from vinta_billing.services.container import get_subscription_service

    get_subscription_service()._sync_limits(subscription, root_plan)
    return subscription


def _owner_client(scope):
    """An authenticated client acting as the owner of ``scope``."""
    client = APIClient()
    client.force_login(scope.owner)
    return client


#: The endpoints whose only tenancy gate is ``check_object_permissions``
#: against the resolved billing root, as ``(url name, method, body)``.
ROOT_GATED_ENDPOINTS = [
    ("billing:BillingUsageOccurrence-list", "get", None),
    ("billing:BillingSubscription-change-plan", "post", {}),
    ("billing:BillingSubscription-cancel", "post", {}),
    ("billing:BillingSubscription-retry-payment", "post", {}),
    ("billing:BillingAddOn-list", "post", {}),
]


class TestAChildScopeCannotActOnItsBillingRoot:
    """The authorization bypass, from the outside.

    Before the fix every one of these answered something other than 403: the
    object-level check read ``getattr(root, "scope", None)``, found that a scope
    has no such field, and fell back to the *request*-level check -- which had
    already passed, because the caller does own their own scope.
    """

    @pytest.mark.parametrize(
        "url_name,method,body",
        ROOT_GATED_ENDPOINTS,
        ids=[name for name, _, _ in ROOT_GATED_ENDPOINTS],
    )
    def test_the_child_is_refused(
        self, url_name, method, body, reseller_root, child, root_subscription
    ):
        client = _owner_client(child)

        response = getattr(client, method)(reverse(url_name), body, format="json")

        assert response.status_code == 403, (
            f"{url_name} answered {response.status_code}: the owner of {child.label!r} reached "
            f"an action gated on {reseller_root.label!r}'s subscription"
        )

    def test_the_child_cannot_change_the_roots_plan(
        self, reseller_root, child, root_subscription, root_plan
    ):
        """The concrete consequence, spelled out: the root's stored plan is
        still the root's plan afterwards."""
        expensive = BillingPlan.objects.create(
            name="Enterprise",
            slug="enterprise",
            monthly_price=Decimal("500.00"),
            annual_price=Decimal("5000.00"),
            is_active=True,
        )
        client = _owner_client(child)

        response = client.post(
            reverse("billing:BillingSubscription-change-plan"),
            {
                "plan_slug": expensive.slug,
                "billing_interval": BillingInterval.MONTHLY,
                "idempotency_key": "child-attempt-1",
            },
            format="json",
        )

        assert response.status_code == 403
        root_subscription.refresh_from_db()
        assert root_subscription.plan_id == root_plan.pk


class TestTheRootsOwnOwnerIsStillAllowedThrough:
    """The other half: the fix must refuse the child without refusing the root.

    Asserting ``!= 403`` rather than a specific success code -- what each of
    these answers past the gate (a 404 for a scope with nothing to cancel, a 400
    for a missing field) is another test's subject; that the permission layer
    did not stop them is this one's.
    """

    @pytest.mark.parametrize(
        "url_name,method,body",
        ROOT_GATED_ENDPOINTS,
        ids=[name for name, _, _ in ROOT_GATED_ENDPOINTS],
    )
    def test_the_owner_of_the_root_passes_the_object_gate(
        self, url_name, method, body, reseller_root, root_subscription
    ):
        client = _owner_client(reseller_root)

        response = getattr(client, method)(reverse(url_name), body, format="json")

        assert response.status_code != 403


class TestScopedObjectsAreUnaffected:
    """The existing behaviour for a genuinely scoped object -- everything with a
    ``scope`` foreign key -- is untouched: the check is still asked about *that*
    object's scope."""

    def test_a_row_belonging_to_another_tenant_is_still_refused(
        self, reseller_root, child, root_subscription
    ):
        from rest_framework.test import APIRequestFactory

        from vinta_billing.permissions import IsBillingManager

        request = APIRequestFactory().get("/")
        request.user = child.owner
        request.scope = child

        assert IsBillingManager().has_object_permission(request, None, root_subscription) is False
