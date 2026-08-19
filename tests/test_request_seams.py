"""Resolving the acting organization, and who may act on its billing.

These are the seams a request passes through, and the ones most likely to fail
open if they are wrong -- a permission that says yes by mistake, or a queryset
that forgets to narrow, hands one tenant another's billing.
"""

import pytest
from django.test import override_settings
from rest_framework.test import APIRequestFactory

from tests.testapp.models import Widget
from vinta_billing.permissions import (
    MANAGE_BILLING_PERMISSION,
    IsBillingManager,
    any_member_may_manage_billing,
    may_manage_billing,
    member_holding_manage_billing,
)
from vinta_billing.recipients import (
    all_members,
    get_billing_recipients,
    members_holding_manage_billing,
)
from vinta_billing.utils import get_organization_state, get_request_organization
from vinta_billing.view_mixins import TenantScopedViewMixin


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
        """Works off the request path too -- a background job that bound one
        around its unit of work, for instance."""
        request = APIRequestFactory().get("/")

        with get_organization_state().context(organization):
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


@pytest.fixture
def manage_billing_permission(db):
    """The permission ``Subscription.Meta`` declares, as an ``auth.Permission`` row."""
    from django.contrib.auth.models import Permission

    app_label, codename = MANAGE_BILLING_PERMISSION.split(".")
    return Permission.objects.get(content_type__app_label=app_label, codename=codename)


class TestPermissionBackedPredicate:
    """`member_holding_manage_billing` -- offered, not the default."""

    def test_a_member_without_the_grant_may_not(self, organization, user, membership):
        """The whole difference from the default predicate: membership alone is
        not enough."""
        assert member_holding_manage_billing(user, organization) is False

    def test_a_member_holding_it_directly_may(
        self, organization, user, membership, manage_billing_permission
    ):
        membership.permissions.add(manage_billing_permission)

        assert member_holding_manage_billing(user, organization) is True

    def test_a_member_holding_it_through_a_group_may(
        self, organization, user, membership, manage_billing_permission
    ):
        from django.contrib.auth.models import Group

        group = Group.objects.create(name="Billing owners")
        group.permissions.add(manage_billing_permission)
        membership.groups.add(group)

        assert member_holding_manage_billing(user, organization) is True

    def test_a_deactivated_member_holding_it_may_not(
        self, organization, user, membership, manage_billing_permission
    ):
        """Deactivation has to withdraw the capability, or it withdraws nothing."""
        membership.permissions.add(manage_billing_permission)
        membership.is_active = False
        membership.save()

        assert member_holding_manage_billing(user, organization) is False

    def test_the_grant_does_not_cross_organizations(
        self, organization, other_organization, user, membership, manage_billing_permission
    ):
        membership.permissions.add(manage_billing_permission)

        assert member_holding_manage_billing(user, other_organization) is False

    def test_a_superuser_who_is_not_a_member_may_not(
        self, organization, other_organization, manage_billing_permission
    ):
        """`has_perm` would say yes here, which is why this does not use it."""
        from django.contrib.auth import get_user_model

        root = get_user_model().objects.create_superuser(username="root", password="pw")

        assert root.has_perm(MANAGE_BILLING_PERMISSION) is True
        assert member_holding_manage_billing(root, organization) is False

    def test_no_organization_means_no(self, user):
        assert member_holding_manage_billing(user, None) is False

    @override_settings(
        VINTA_BILLING={
            "BILLING_MANAGER_PREDICATE": ("vinta_billing.permissions.member_holding_manage_billing")
        }
    )
    def test_a_project_can_select_it(
        self, organization, user, membership, manage_billing_permission
    ):
        from django.contrib.auth import get_user_model

        assert may_manage_billing(user, organization) is False

        membership.permissions.add(manage_billing_permission)
        # A fresh instance, like the next request would carry: the backend
        # memoizes an organization's permissions on the user object it was asked
        # about, exactly as Django's own `ModelBackend` does.
        granted = get_user_model().objects.get(pk=user.pk)

        assert may_manage_billing(granted, organization) is True


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


class TestPermissionBackedRecipients:
    def test_only_members_holding_the_grant_are_told(
        self, organization, user, membership, manage_billing_permission
    ):
        assert list(members_holding_manage_billing(organization)) == []

        membership.permissions.add(manage_billing_permission)

        assert list(members_holding_manage_billing(organization)) == [user.pk]

    def test_a_deactivated_member_is_not_told(
        self, organization, user, membership, manage_billing_permission
    ):
        membership.permissions.add(manage_billing_permission)
        membership.is_active = False
        membership.save()

        assert list(members_holding_manage_billing(organization)) == []

    def test_a_member_is_listed_once_however_many_groups_carry_it(
        self, organization, user, membership, manage_billing_permission
    ):
        """Both paths are multi-valued joins, so the duplicate is the default."""
        from django.contrib.auth.models import Group

        for name in ("Owners", "Admins"):
            group = Group.objects.create(name=name)
            group.permissions.add(manage_billing_permission)
            membership.groups.add(group)
        membership.permissions.add(manage_billing_permission)

        assert list(members_holding_manage_billing(organization)) == [user.pk]

    def test_it_does_not_cross_organizations(
        self, organization, other_organization, user, membership, manage_billing_permission
    ):
        membership.permissions.add(manage_billing_permission)

        assert list(members_holding_manage_billing(other_organization)) == []


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

        with get_organization_state().context(organization):
            view.initial(request)

        assert request.organization == organization

    def test_get_organization_reads_what_initial_stamped(self, organization):
        view = TenantScopedViewMixin()
        view.request = APIRequestFactory().get("/")
        view.request.organization = organization

        assert view.get_organization() is organization
