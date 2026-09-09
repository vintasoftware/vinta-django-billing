"""Who may read and change a scope's billing.

The application this was extracted from answered that with a role column and an
``is_billing_owner`` flag on its membership model. A scope has no members --
it may not even be an organization -- so the question is delegated to a
project-supplied predicate and the DRF permission classes here are written
against it.

    # settings.py
    VINTA_BILLING = {'BILLING_MANAGER_PREDICATE': 'myproject.billing.is_billing_owner'}

    # myproject/billing.py
    def is_billing_owner(user, scope):
        return Membership.objects.filter(
            user=user, organization_id=scope.object_id, is_billing_owner=True
        ).exists()

The shipped default reads ``scope.owner``; see
:func:`owner_may_manage_billing`.
"""

from __future__ import annotations

from typing import Any, cast

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from vinta_billing.conf import get_object_from_setting
from vinta_billing.models import AbstractBillingScope
from vinta_billing.utils import get_request_scope


MANAGE_BILLING_PERMISSION = "vinta_billing.manage_billing"


def owner_may_manage_billing(user: Any, scope: AbstractBillingScope | None) -> bool:
    """The default predicate: the scope's owner, and nobody else.

    Least privilege, and the answer that is right without configuration for the
    case that motivated scopes in the first place -- a personal plan, where the
    owner *is* the payer and ``BillingScope.objects.get_or_create_for(user)``
    has already set it.

    For an organization scope this is deliberately strict: nothing here can know
    which member of an organization owns its billing, and reading the owner off
    a column a project has not populated returns ``False`` rather than opening
    the endpoint to every member. A project with memberships configures
    ``BILLING_MANAGER_PREDICATE``; one with a single billing contact per tenant
    just sets ``scope.owner``.
    """
    if scope is None or user is None or not user.is_authenticated:
        return False
    # ``owner_id``, not ``owner``: no query, and an unsaved user compares as
    # ``None == None`` through the attribute but not through the column.
    return scope.owner_id is not None and scope.owner_id == user.pk


def may_manage_billing(user: Any, scope: AbstractBillingScope | None) -> bool:
    """Run the configured predicate."""
    predicate = get_object_from_setting("BILLING_MANAGER_PREDICATE")
    return bool(predicate(user, scope))


class IsBillingManager(BasePermission):
    """Guards every endpoint that reads or changes billing.

    Resolves the scope from the request the same way the tenant-scoped view
    mixin does, then defers to the configured predicate.

    The object-level check answers the same question about the object instead
    of about the request -- about the object's own ``scope`` for a scoped row,
    and about the object itself when it *is* a scope, which is what every
    object-level check the shipped viewsets make passes (the resolved billing
    root). See :meth:`has_object_permission`.
    """

    message = "You do not have permission to manage this scope's billing."

    def has_permission(self, request: Request, view: APIView) -> bool:
        # `get_request_scope` is typed against the base `Model` because a
        # project's resolver is free to return any model.
        scope = cast("AbstractBillingScope | None", get_request_scope(request))
        return may_manage_billing(request.user, scope)

    def has_object_permission(self, request: Request, view: APIView, obj: Any) -> bool:
        # A scope *is* the object every one of this package's own object-level
        # checks passes: `MeteredOccurrenceViewSet.list`,
        # `SubscriptionViewSet.get_subscription` and `AddOnViewSet.create` all
        # hand over the resolved billing root, because "may this caller act on
        # this root's billing?" is what those actions actually need answered.
        # A scope has no `scope` field, so reading one off it found nothing and
        # dropped through to `has_permission` -- the request-level check, taken
        # against the scope the *request* resolved, which the caller had already
        # passed. The gate those call sites call "the real gate" therefore
        # decided nothing at all, and an administrator of a child scope could
        # change a reseller root's plan, cancel its subscription and buy add-ons
        # billed to it.
        #
        # Asked of `AbstractBillingScope`, not of the concrete class
        # `conf.get_scope_model()` returns: a project that swapped
        # `BILLING_SCOPE_MODEL` has a scope model of its own, and the abstract
        # base is the one thing every such model inherits.
        if isinstance(obj, AbstractBillingScope):
            return may_manage_billing(request.user, obj)

        # Everything else in this app is scoped, so the object-level check is
        # the same question asked about the object's own scope -- which stops a
        # correctly-scoped user from reaching another payer's row through a
        # guessed URL.
        scope = getattr(obj, "scope", None)
        if scope is None:
            return self.has_permission(request, view)
        return may_manage_billing(request.user, scope)
