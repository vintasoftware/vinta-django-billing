"""Who may read and change an organization's billing.

The application this was extracted from answered that with a role column and an
``is_billing_owner`` flag on its membership model. ``vinta-django-orgs``'
membership model has neither, so the question is delegated to a project-supplied
predicate and the DRF permission classes here are written against it.

    # settings.py
    VINTA_BILLING = {'BILLING_MANAGER_PREDICATE': 'myproject.billing.is_billing_owner'}

    # myproject/billing.py
    def is_billing_owner(user, organization):
        return Membership.objects.filter(
            user=user, organization=organization, is_billing_owner=True
        ).exists()
"""

from __future__ import annotations

from typing import Any, cast

from organizations.models import Organization
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from billing.conf import get_object_from_setting


def any_member_may_manage_billing(user: Any, organization: Organization | None) -> bool:
    """The default predicate: any member of the organization may manage billing.

    The most permissive answer that is still tenant-safe -- it never lets one
    organization's member touch another's billing, but it draws no distinction
    between an owner and an ordinary member. Projects that have that distinction
    should configure a narrower predicate; this default exists so the shipped
    endpoints work out of the box rather than 403-ing until something is wired.

    Reads the membership model through ``vinta-django-orgs``, whose membership
    manager is deliberately unscoped -- membership is metadata *about* the
    tenancy, so scoping it to the selected organization would be circular.
    """
    if organization is None or user is None or not user.is_authenticated:
        return False

    from organizations.conf import get_organization_membership_model

    membership_model = get_organization_membership_model()
    return membership_model.objects.filter(user=user, organization=organization).exists()


def may_manage_billing(user: Any, organization: Organization | None) -> bool:
    """Run the configured predicate."""
    predicate = get_object_from_setting("BILLING_MANAGER_PREDICATE")
    return bool(predicate(user, organization))


class IsBillingManager(BasePermission):
    """Guards every endpoint that reads or changes billing.

    Resolves the organization from the request the same way the tenant-scoped
    view mixin does, then defers to the configured predicate.
    """

    message = "You do not have permission to manage this organization's billing."

    def has_permission(self, request: Request, view: APIView) -> bool:
        from billing.utils import get_request_organization

        # `get_request_organization` is typed against the base `Model` because
        # it also reads the context variable, which is not narrowed there.
        organization = cast("Organization | None", get_request_organization(request))
        return may_manage_billing(request.user, organization)

    def has_object_permission(self, request: Request, view: APIView, obj: Any) -> bool:
        # Objects in this app are organization-scoped, so the object-level check
        # is the same question asked about the object's own organization --
        # which stops a correctly-scoped user from reaching another tenant's row
        # through a guessed URL.
        organization = getattr(obj, "organization", None)
        if organization is None:
            return self.has_permission(request, view)
        return may_manage_billing(request.user, organization)
