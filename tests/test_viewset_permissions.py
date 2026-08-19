"""The object-level billing gate, exercised through the mounted viewsets.

``tests/test_request_seams.py`` tests ``IsBillingManager`` in isolation, by
calling ``has_object_permission`` with an object it built. That is exactly how
an authorization bypass survived two releases: every object-level check the
shipped viewsets make passes a **billing root**, which is an ``Organization``,
and nothing in the suite ever sent a request through one of those viewsets to
find out what the permission class did with it.

So the tests here send real requests, through the router the README tells a
project to mount, to the three endpoints that check against a resolved billing
root:

* ``GET  /billing/usage/occurrences/`` (``MeteredOccurrenceViewSet.list``)
* ``POST /billing/subscription/change-plan/`` and its two sibling write actions
  (``SubscriptionViewSet.get_subscription(check_object_perms=True)``)
* ``POST /billing/add-ons/`` (``AddOnViewSet.create``)

The caller in each is an administrator of a *child* organization that bills
against a reseller root it has no membership in. The request-level check passes
-- they do administer something -- and the object-level check is the only thing
standing between them and the root's plan, the root's payment method and the
root's usage.
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
from vinta_orgs.conf import get_organization_membership_model, get_organization_model

from vinta_billing.constants import BillingInterval, BillingState, LimitKind
from vinta_billing.hierarchy import FlatHierarchy
from vinta_billing.models import BillingPlan, PlanLimit, Subscription


pytestmark = pytest.mark.django_db


class ResellerHierarchy(FlatHierarchy):
    """One reseller root, and every other organization billing against it.

    The stock ``vinta-django-orgs`` organization model has no parent field, so a
    project's hierarchy is the only thing that can say a child bills against an
    ancestor -- which is what ``HIERARCHY`` is for. This is the smallest
    strategy that produces the shape the defect lives in.
    """

    root_slug = "reseller-root"

    def is_billing_root(self, organization):
        return organization.slug == self.root_slug

    def resolve_billing_root(self, organization):
        if self.is_billing_root(organization):
            return organization
        return get_organization_model().objects.get(slug=self.root_slug)

    def pooled_organization_ids(self, root):
        return list(get_organization_model().objects.values_list("pk", flat=True))


#: Applied to every test below. ``HIERARCHY`` is handed the class itself rather
#: than a dotted path, which ``get_object_from_setting`` supports for exactly
#: this.
reseller_settings = override_settings(VINTA_BILLING={"HIERARCHY": ResellerHierarchy})


@pytest.fixture(autouse=True)
def _reseller_hierarchy():
    with reseller_settings:
        yield


@pytest.fixture
def reseller_root(db):
    return get_organization_model().objects.create(name="Reseller", slug="reseller-root")


@pytest.fixture
def child(db):
    """An organization that bills against ``reseller_root`` and administers itself."""
    return get_organization_model().objects.create(name="Child", slug="child")


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
    """The subscription the child organization must not be able to touch."""
    now = timezone.now()
    subscription = Subscription.objects.create(
        organization=reseller_root,
        plan=root_plan,
        billing_state=BillingState.ACTIVE,
        billing_interval=BillingInterval.MONTHLY,
        current_period_start=now - datetime.timedelta(days=1),
        current_period_end=now + datetime.timedelta(days=29),
    )
    from vinta_billing.services.container import get_subscription_service

    get_subscription_service()._sync_limits(subscription, root_plan)
    return subscription


def _member_client(organization):
    """An authenticated client acting as a member of ``organization``.

    The organization is selected with the ``Organization-Slug`` header, which
    ``vinta-django-orgs``' stock retrievers read and -- under
    ``VERIFY_ORGANIZATION_MEMBERSHIP``, on by default -- refuse for a caller who
    is not a member. So this really is "an administrator of this organization,
    acting on it", not a caller naming somebody else's tenant.
    """
    user = get_user_model().objects.create_user(
        username=f"member-of-{organization.slug}", password="pw"
    )
    get_organization_membership_model().objects.create(organization=organization, user=user)
    client = APIClient()
    client.force_login(user)
    client.credentials(HTTP_ORGANIZATION_SLUG=organization.slug)
    return client


#: The three endpoints whose only tenancy gate is ``check_object_permissions``
#: against the resolved billing root, as ``(url name, method, body)``.
ROOT_GATED_ENDPOINTS = [
    ("billing:BillingUsageOccurrence-list", "get", None),
    ("billing:BillingSubscription-change-plan", "post", {}),
    ("billing:BillingSubscription-cancel", "post", {}),
    ("billing:BillingSubscription-retry-payment", "post", {}),
    ("billing:BillingAddOn-list", "post", {}),
]


class TestAChildOrganizationCannotActOnItsBillingRoot:
    """The authorization bypass, from the outside.

    Before the fix every one of these answered something other than 403: the
    object-level check read ``getattr(root, "organization", None)``, found an
    ``Organization`` has no such field, and fell back to the *request*-level
    check -- which had already passed, because the caller does administer their
    own organization.
    """

    @pytest.mark.parametrize(
        "url_name,method,body",
        ROOT_GATED_ENDPOINTS,
        ids=[name for name, _, _ in ROOT_GATED_ENDPOINTS],
    )
    def test_the_child_is_refused(
        self, url_name, method, body, reseller_root, child, root_subscription
    ):
        client = _member_client(child)

        response = getattr(client, method)(reverse(url_name), body, format="json")

        assert response.status_code == 403, (
            f"{url_name} answered {response.status_code}: a member of {child.slug!r} reached "
            f"an action gated on {reseller_root.slug!r}'s subscription"
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
        client = _member_client(child)

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


class TestTheRootsOwnMemberIsStillAllowedThrough:
    """The other half: the fix must refuse the child without refusing the root.

    Asserting ``!= 403`` rather than a specific success code -- what each of
    these answers past the gate (a 404 for an organization with nothing to
    cancel, a 400 for a missing field) is another test's subject; that the
    permission layer did not stop them is this one's.
    """

    @pytest.mark.parametrize(
        "url_name,method,body",
        ROOT_GATED_ENDPOINTS,
        ids=[name for name, _, _ in ROOT_GATED_ENDPOINTS],
    )
    def test_a_member_of_the_root_passes_the_object_gate(
        self, url_name, method, body, reseller_root, root_subscription
    ):
        client = _member_client(reseller_root)

        response = getattr(client, method)(reverse(url_name), body, format="json")

        assert response.status_code != 403


class TestOrganizationScopedObjectsAreUnaffected:
    """The existing behaviour for a genuinely organization-scoped object --
    everything with an ``organization`` foreign key -- is untouched: the check
    is still asked about *that* object's organization."""

    def test_a_row_belonging_to_another_tenant_is_still_refused(
        self, reseller_root, child, root_subscription
    ):
        from rest_framework.test import APIRequestFactory

        from vinta_billing.permissions import IsBillingManager

        request = APIRequestFactory().get("/")
        request.user = get_user_model().objects.create_user(username="outsider", password="pw")
        get_organization_membership_model().objects.create(organization=child, user=request.user)
        request.organization = child

        assert IsBillingManager().has_object_permission(request, None, root_subscription) is False
