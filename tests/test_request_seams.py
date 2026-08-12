"""Resolving the acting organization, and who may act on its billing.

These are the seams a request passes through, and the ones most likely to fail
open if they are wrong -- a permission that says yes by mistake, or a queryset
that forgets to narrow, hands one tenant another's billing.
"""

import pytest
from django.test import override_settings
from organizations.state import organization_context
from rest_framework.test import APIRequestFactory

from billing.permissions import (
    IsBillingManager,
    any_member_may_manage_billing,
    may_manage_billing,
)
from billing.recipients import all_members, get_billing_recipients
from billing.utils import get_request_organization
from billing.view_mixins import TenantScopedViewMixin
from tests.testapp.models import Widget


pytestmark = pytest.mark.django_db


def deny_everyone(user, organization):
    return False


def allow_everyone(user, organization):
    return True


class TestGetRequestOrganization:
    def test_prefers_what_the_view_already_resolved(self, organization):
        request = APIRequestFactory().get("/")
        request.organization = organization

        assert get_request_organization(request) is organization

    def test_falls_back_to_the_bound_context(self, organization):
        """Works off the request path too -- a Celery task under
        `organization_context`, for instance."""
        request = APIRequestFactory().get("/")

        with organization_context(organization):
            assert get_request_organization(request) == organization

    def test_is_none_when_nothing_is_bound(self):
        assert get_request_organization(APIRequestFactory().get("/")) is None


class TestDefaultPredicate:
    def test_a_member_may_manage_billing(self, organization, user, membership):
        assert any_member_may_manage_billing(user, organization) is True

    def test_a_non_member_may_not(self, organization, user):
        """The default is permissive about *roles*, never about tenancy."""
        assert any_member_may_manage_billing(user, organization) is False

    def test_a_member_of_another_organization_may_not(
        self, organization, other_organization, user, membership
    ):
        assert any_member_may_manage_billing(user, other_organization) is False

    def test_an_anonymous_user_may_not(self, organization):
        from django.contrib.auth.models import AnonymousUser

        assert any_member_may_manage_billing(AnonymousUser(), organization) is False

    def test_no_organization_means_no(self, user):
        assert any_member_may_manage_billing(user, None) is False


class TestConfiguredPredicate:
    @override_settings(
        VINTA_BILLING={"BILLING_MANAGER_PREDICATE": "tests.test_request_seams.deny_everyone"}
    )
    def test_the_setting_replaces_the_default(self, organization, user, membership):
        assert may_manage_billing(user, organization) is False

    @override_settings(
        VINTA_BILLING={"BILLING_MANAGER_PREDICATE": "tests.test_request_seams.allow_everyone"}
    )
    def test_a_project_can_widen_it_too(self, organization, user):
        assert may_manage_billing(user, organization) is True


class TestIsBillingManager:
    def _request(self, user, organization=None):
        request = APIRequestFactory().get("/")
        request.user = user
        if organization is not None:
            request.organization = organization
        return request

    def test_allows_a_member(self, organization, user, membership):
        assert IsBillingManager().has_permission(self._request(user, organization), None) is True

    def test_refuses_a_non_member(self, organization, user):
        assert IsBillingManager().has_permission(self._request(user, organization), None) is False

    def test_object_permission_asks_about_the_objects_own_organization(
        self, organization, other_organization, user, membership
    ):
        """Stops a correctly-scoped user reaching another tenant's row through a
        guessed URL."""
        theirs = Widget.objects.create(organization=other_organization, name="theirs")
        request = self._request(user, organization)

        assert IsBillingManager().has_object_permission(request, None, theirs) is False

    def test_object_permission_allows_the_users_own_organizations_row(
        self, organization, user, membership
    ):
        mine = Widget.objects.create(organization=organization, name="mine")
        request = self._request(user, organization)

        assert IsBillingManager().has_object_permission(request, None, mine) is True

    def test_an_object_with_no_organization_falls_back_to_the_request(
        self, organization, user, membership
    ):
        request = self._request(user, organization)

        assert IsBillingManager().has_object_permission(request, None, object()) is True


class TestTenantScopedViewMixin:
    def _view(self, organization, required=True):
        view = TenantScopedViewMixin()
        view.organization_required = required
        request = APIRequestFactory().get("/")
        request.organization = organization
        view.request = request
        return view

    def test_narrows_to_the_acting_organization(self, organization, other_organization):
        Widget.objects.create(organization=organization, name="mine")
        Widget.objects.create(organization=other_organization, name="theirs")

        filtered = self._view(organization).filter_queryset_by_organization(
            Widget.original_manager.all()
        )

        assert [widget.name for widget in filtered] == ["mine"]

    def test_fails_closed_when_no_organization_resolved(self, organization):
        """The alternative leaks every tenant's billing rows to a caller whose
        organization simply failed to resolve."""
        Widget.objects.create(organization=organization, name="mine")

        filtered = self._view(None).filter_queryset_by_organization(Widget.original_manager.all())

        assert list(filtered) == []

    def test_an_organization_independent_view_sees_everything(
        self, organization, other_organization
    ):
        Widget.objects.create(organization=organization, name="mine")
        Widget.objects.create(organization=other_organization, name="theirs")

        filtered = self._view(None, required=False).filter_queryset_by_organization(
            Widget.original_manager.all()
        )

        assert filtered.count() == 2


class TestRecipients:
    def test_the_default_is_every_member(self, organization, user, membership):
        assert list(all_members(organization)) == [user.pk]

    def test_members_of_other_organizations_are_excluded(
        self, organization, other_organization, user, membership
    ):
        assert list(all_members(other_organization)) == []

    @override_settings(
        VINTA_BILLING={"BILLING_RECIPIENTS": "tests.test_request_seams.no_recipients"}
    )
    def test_a_project_can_narrow_it(self, organization, user, membership):
        assert list(get_billing_recipients(organization)) == []


def no_recipients(organization):
    return []


class TestViewMixinResolution:
    def test_initial_stamps_the_organization_onto_the_request(self, organization):
        """`initial()` runs after DRF authentication, which is the earliest point
        a retriever depending on `request.user` can work."""

        class View(TenantScopedViewMixin):
            def initial(self, request, *args, **kwargs):
                # Stands in for `APIView.initial`, which this mixin calls first.
                request.organization = self.resolve_organization(request)

        request = APIRequestFactory().get("/")
        view = View()

        with organization_context(organization):
            view.initial(request)

        assert request.organization == organization

    def test_get_organization_reads_what_initial_stamped(self, organization):
        view = TenantScopedViewMixin()
        view.request = APIRequestFactory().get("/")
        view.request.organization = organization

        assert view.get_organization() is organization
