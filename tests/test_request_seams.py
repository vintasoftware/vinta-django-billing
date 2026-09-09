"""Resolving the acting scope, and who may act on its billing.

These are the seams a request passes through, and the ones most likely to fail
open if they are wrong -- a permission that says yes by mistake, or a queryset
that forgets to narrow, hands one tenant another's billing.
"""

import pytest
from django.test import override_settings
from rest_framework.test import APIRequestFactory

from tests.payers import scopes_can_name_users
from tests.testapp.models import Widget
from vinta_billing.contrib.orgs import (
    all_members,
    any_member_may_manage_billing,
    member_holding_manage_billing,
    members_holding_manage_billing,
)
from vinta_billing.permissions import (
    MANAGE_BILLING_PERMISSION,
    IsBillingManager,
    may_manage_billing,
)
from vinta_billing.recipients import get_billing_recipients
from vinta_billing.utils import get_request_scope
from vinta_billing.view_mixins import TenantScopedViewMixin


pytestmark = pytest.mark.django_db


def deny_everyone(user, scope):
    return False


def allow_everyone(user, scope):
    return True


class TestGetRequestScope:
    def test_prefers_what_the_view_already_resolved(self, scope):
        request = APIRequestFactory().get("/")
        request.scope = scope

        assert get_request_scope(request) is scope

    @pytest.mark.skipif(
        not scopes_can_name_users(),
        reason="the configured scope model bills companies only",
    )
    def test_falls_back_to_the_scope_the_caller_owns(self, db, user):
        """A personal plan resolves with nothing configured at all.

        The shipped resolver asks the scope manager's ``scope_for`` hook, which
        is what makes "bill this user" work before a project has written a
        resolver of its own.
        """
        from tests.conftest import make_scope

        personal = make_scope(user)
        request = APIRequestFactory().get("/")
        request.user = user

        assert get_request_scope(request) == personal

    def test_a_configured_resolver_wins(self, scope, settings):
        """The seam a project with its own tenancy reaches for."""
        request = APIRequestFactory().get("/")

        with override_settings(VINTA_BILLING={"SCOPE_RESOLVER": lambda _request: scope}):
            assert get_request_scope(request) is scope

    def test_is_none_when_nothing_is_bound(self):
        assert get_request_scope(APIRequestFactory().get("/")) is None


class TestDefaultPredicate:
    def test_a_member_may_manage_billing(self, scope, user, membership):
        assert any_member_may_manage_billing(user, scope) is True

    def test_a_non_member_may_not(self, scope, user):
        """The default is permissive about *roles*, never about tenancy."""
        assert any_member_may_manage_billing(user, scope) is False

    def test_a_member_of_another_organization_may_not(self, scope, other_scope, user, membership):
        assert any_member_may_manage_billing(user, other_scope) is False

    def test_an_anonymous_user_may_not(self, scope):
        from django.contrib.auth.models import AnonymousUser

        assert any_member_may_manage_billing(AnonymousUser(), scope) is False

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

    def test_a_member_without_the_grant_may_not(self, scope, user, membership):
        """The whole difference from the default predicate: membership alone is
        not enough."""
        assert member_holding_manage_billing(user, scope) is False

    def test_a_member_holding_it_directly_may(
        self, scope, user, membership, manage_billing_permission
    ):
        membership.permissions.add(manage_billing_permission)

        assert member_holding_manage_billing(user, scope) is True

    def test_a_member_holding_it_through_a_group_may(
        self, scope, user, membership, manage_billing_permission
    ):
        from django.contrib.auth.models import Group

        group = Group.objects.create(name="Billing owners")
        group.permissions.add(manage_billing_permission)
        membership.groups.add(group)

        assert member_holding_manage_billing(user, scope) is True

    def test_a_deactivated_member_holding_it_may_not(
        self, scope, user, membership, manage_billing_permission
    ):
        """Deactivation has to withdraw the capability, or it withdraws nothing."""
        membership.permissions.add(manage_billing_permission)
        membership.is_active = False
        membership.save()

        assert member_holding_manage_billing(user, scope) is False

    def test_the_grant_does_not_cross_organizations(
        self, scope, other_scope, user, membership, manage_billing_permission
    ):
        membership.permissions.add(manage_billing_permission)

        assert member_holding_manage_billing(user, other_scope) is False

    def test_a_superuser_who_is_not_a_member_may_not(
        self, scope, other_scope, manage_billing_permission
    ):
        """`has_perm` would say yes here, which is why this does not use it."""
        from django.contrib.auth import get_user_model

        root = get_user_model().objects.create_superuser(username="root", password="pw")

        assert root.has_perm(MANAGE_BILLING_PERMISSION) is True
        assert member_holding_manage_billing(root, scope) is False

    def test_no_organization_means_no(self, user):
        assert member_holding_manage_billing(user, None) is False

    @override_settings(
        VINTA_BILLING={
            "BILLING_MANAGER_PREDICATE": (
                "vinta_billing.contrib.orgs.member_holding_manage_billing"
            )
        }
    )
    def test_a_project_can_select_it(self, scope, user, membership, manage_billing_permission):
        from django.contrib.auth import get_user_model

        assert may_manage_billing(user, scope) is False

        membership.permissions.add(manage_billing_permission)
        # A fresh instance, like the next request would carry: the backend
        # memoizes an scope's permissions on the user object it was asked
        # about, exactly as Django's own `ModelBackend` does.
        granted = get_user_model().objects.get(pk=user.pk)

        assert may_manage_billing(granted, scope) is True


class TestConfiguredPredicate:
    @override_settings(
        VINTA_BILLING={"BILLING_MANAGER_PREDICATE": "tests.test_request_seams.deny_everyone"}
    )
    def test_the_setting_replaces_the_default(self, scope, user, membership):
        assert may_manage_billing(user, scope) is False

    @override_settings(
        VINTA_BILLING={"BILLING_MANAGER_PREDICATE": "tests.test_request_seams.allow_everyone"}
    )
    def test_a_project_can_widen_it_too(self, scope, user):
        assert may_manage_billing(user, scope) is True


class TestIsBillingManager:
    def _request(self, user, scope=None):
        request = APIRequestFactory().get("/")
        request.user = user
        if scope is not None:
            request.scope = scope
        return request

    def test_allows_a_member(self, scope, user, membership):
        assert IsBillingManager().has_permission(self._request(user, scope), None) is True

    def test_refuses_a_caller_who_does_not_own_the_scope(self, other_scope, user):
        assert IsBillingManager().has_permission(self._request(user, other_scope), None) is False

    def test_object_permission_asks_about_the_objects_own_organization(
        self, scope, other_scope, user, membership
    ):
        """Stops a correctly-scoped user reaching another tenant's row through a
        guessed URL."""
        theirs = Widget.objects.create(scope=other_scope, name="theirs")
        request = self._request(user, scope)

        assert IsBillingManager().has_object_permission(request, None, theirs) is False

    def test_object_permission_allows_the_users_own_organizations_row(
        self, scope, user, membership
    ):
        mine = Widget.objects.create(scope=scope, name="mine")
        request = self._request(user, scope)

        assert IsBillingManager().has_object_permission(request, None, mine) is True

    def test_an_object_with_no_organization_falls_back_to_the_request(
        self, scope, user, membership
    ):
        request = self._request(user, scope)

        assert IsBillingManager().has_object_permission(request, None, object()) is True


class TestTenantScopedViewMixin:
    def _view(self, scope, required=True):
        view = TenantScopedViewMixin()
        view.scope_required = required
        request = APIRequestFactory().get("/")
        request.scope = scope
        view.request = request
        return view

    def test_narrows_to_the_acting_organization(self, scope, other_scope):
        Widget.objects.create(scope=scope, name="mine")
        Widget.objects.create(scope=other_scope, name="theirs")

        filtered = self._view(scope).filter_queryset_by_scope(Widget.objects.all())

        assert [widget.name for widget in filtered] == ["mine"]

    def test_fails_closed_when_no_organization_resolved(self, scope):
        """The alternative leaks every tenant's billing rows to a caller whose
        scope simply failed to resolve."""
        Widget.objects.create(scope=scope, name="mine")

        filtered = self._view(None).filter_queryset_by_scope(Widget.objects.all())

        assert list(filtered) == []

    def test_an_organization_independent_view_sees_everything(self, scope, other_scope):
        Widget.objects.create(scope=scope, name="mine")
        Widget.objects.create(scope=other_scope, name="theirs")

        filtered = self._view(None, required=False).filter_queryset_by_scope(Widget.objects.all())

        assert filtered.count() == 2


class TestRecipients:
    def test_the_default_is_every_member(self, scope, user, membership):
        assert list(all_members(scope)) == [user.pk]

    def test_members_of_other_organizations_are_excluded(
        self, scope, other_scope, user, membership
    ):
        assert list(all_members(other_scope)) == []

    @override_settings(
        VINTA_BILLING={"BILLING_RECIPIENTS": "tests.test_request_seams.no_recipients"}
    )
    def test_a_project_can_narrow_it(self, scope, user, membership):
        assert list(get_billing_recipients(scope)) == []


def no_recipients(scope):
    return []


class TestPermissionBackedRecipients:
    def test_only_members_holding_the_grant_are_told(
        self, scope, user, membership, manage_billing_permission
    ):
        assert list(members_holding_manage_billing(scope)) == []

        membership.permissions.add(manage_billing_permission)

        assert list(members_holding_manage_billing(scope)) == [user.pk]

    def test_a_deactivated_member_is_not_told(
        self, scope, user, membership, manage_billing_permission
    ):
        membership.permissions.add(manage_billing_permission)
        membership.is_active = False
        membership.save()

        assert list(members_holding_manage_billing(scope)) == []

    def test_a_member_is_listed_once_however_many_groups_carry_it(
        self, scope, user, membership, manage_billing_permission
    ):
        """Both paths are multi-valued joins, so the duplicate is the default."""
        from django.contrib.auth.models import Group

        for name in ("Owners", "Admins"):
            group = Group.objects.create(name=name)
            group.permissions.add(manage_billing_permission)
            membership.groups.add(group)
        membership.permissions.add(manage_billing_permission)

        assert list(members_holding_manage_billing(scope)) == [user.pk]

    def test_it_does_not_cross_organizations(
        self, scope, other_scope, user, membership, manage_billing_permission
    ):
        membership.permissions.add(manage_billing_permission)

        assert list(members_holding_manage_billing(other_scope)) == []


class TestViewMixinResolution:
    def test_initial_stamps_the_scope_onto_the_request(self, scope):
        """`initial()` runs after DRF authentication, which is the earliest point
        a resolver depending on `request.user` can work."""

        class View(TenantScopedViewMixin):
            def initial(self, request, *args, **kwargs):
                # Stands in for `APIView.initial`, which this mixin calls first.
                request.scope = self.resolve_scope(request)

        request = APIRequestFactory().get("/")
        view = View()

        with override_settings(VINTA_BILLING={"SCOPE_RESOLVER": lambda _request: scope}):
            view.initial(request)

        assert request.scope == scope

    def test_get_scope_reads_what_initial_stamped(self, scope):
        view = TenantScopedViewMixin()
        view.request = APIRequestFactory().get("/")
        view.request.scope = scope

        assert view.get_scope() is scope
