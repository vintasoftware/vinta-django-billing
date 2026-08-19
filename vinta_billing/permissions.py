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

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView
from vinta_orgs.authorization import has_organization_permission
from vinta_orgs.conf import get_organization_membership_model
from vinta_orgs.models import AbstractOrganization

from vinta_billing.conf import get_object_from_setting
from vinta_billing.utils import get_request_organization


def any_member_may_manage_billing(user: Any, organization: AbstractOrganization | None) -> bool:
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

    membership_model = get_organization_membership_model()
    return membership_model.objects.filter(user=user, organization_id=organization.pk).exists()


MANAGE_BILLING_PERMISSION = "vinta_billing.manage_billing"


def member_holding_manage_billing(user: Any, organization: AbstractOrganization | None) -> bool:
    """A stricter predicate: the member must hold ``vinta_billing.manage_billing``.

    Offered rather than defaulted to. The permission is declared on
    ``Subscription`` but granted by nobody here, so making this the default would
    403 every billing endpoint in a project that has not seeded a group carrying
    it -- including every project upgrading from a version where any member
    could manage billing. Point ``BILLING_MANAGER_PREDICATE`` at it once the
    grant exists::

        VINTA_BILLING = {
            "BILLING_MANAGER_PREDICATE": (
                "vinta_billing.permissions.member_holding_manage_billing"
            ),
        }

    Asks ``vinta-django-orgs``' organization-scoped question, not
    ``user.has_perm``: the latter answers for whichever organization is *bound*,
    unions in the user's global permissions and groups, and says yes to every
    superuser. Billing is routinely read against a reseller **root** that is an
    ancestor of the bound organization, so all three would answer a question
    nobody asked.
    """
    if organization is None:
        return False
    return has_organization_permission(user, MANAGE_BILLING_PERMISSION, organization)


def may_manage_billing(user: Any, organization: AbstractOrganization | None) -> bool:
    """Run the configured predicate."""
    predicate = get_object_from_setting("BILLING_MANAGER_PREDICATE")
    return bool(predicate(user, organization))


class IsBillingManager(BasePermission):
    """Guards every endpoint that reads or changes billing.

    Resolves the organization from the request the same way the tenant-scoped
    view mixin does, then defers to the configured predicate.

    The object-level check answers the same question about the object instead
    of about the request -- about the object's own ``organization`` for an
    organization-scoped row, and about the object itself when it *is* an
    organization, which is what every object-level check the shipped viewsets
    make passes (the resolved billing root). See
    :meth:`has_object_permission`.
    """

    message = "You do not have permission to manage this organization's billing."

    def has_permission(self, request: Request, view: APIView) -> bool:
        # `get_request_organization` is typed against the base `Model` because
        # it also reads the context variable, which is not narrowed there.
        organization = cast("AbstractOrganization | None", get_request_organization(request))
        return may_manage_billing(request.user, organization)

    def has_object_permission(self, request: Request, view: APIView, obj: Any) -> bool:
        # An organization *is* the object every one of this package's own
        # object-level checks passes: `MeteredOccurrenceViewSet.list`,
        # `SubscriptionViewSet.get_subscription` and `AddOnViewSet.create` all
        # hand over the resolved billing root, because "may this caller act on
        # this root's billing?" is what those actions actually need answered.
        # An organization has no `organization` field, so reading one off it
        # found nothing and dropped through to `has_permission` -- the
        # request-level check, taken against the organization the *request*
        # resolved, which the caller had already passed. The gate those call
        # sites call "the real gate" therefore decided nothing at all, and an
        # administrator of a child organization could change a reseller root's
        # plan, cancel its subscription and buy add-ons billed to it.
        #
        # Asked of `AbstractOrganization`, not of the concrete class
        # `get_organization_model()` returns: a project that swapped
        # `ORGANIZATION_MODEL` has an organization model of its own, and the
        # abstract base is the one thing every such model inherits.
        if isinstance(obj, AbstractOrganization):
            return may_manage_billing(request.user, obj)

        # Everything else in this app is organization-scoped, so the
        # object-level check is the same question asked about the object's own
        # organization -- which stops a correctly-scoped user from reaching
        # another tenant's row through a guessed URL.
        organization = getattr(obj, "organization", None)
        if organization is None:
            return self.has_permission(request, view)
        return may_manage_billing(request.user, organization)
