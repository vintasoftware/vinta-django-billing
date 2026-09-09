"""Membership-backed billing policy for projects on ``vinta-django-orgs``.

Everything in here used to be the shipped default. It moved out when billing
stopped depending on organizations: a scope may name a user, a workspace or
anything else, so "every member of it" is not a question the package can ask on
its own any more.

Nothing else in ``vinta_billing`` imports this module, and ``vinta_orgs`` is
imported inside each function rather than at module scope, so a project without
the dependency can still import the package that contains it. Install the extra
to use it::

    pip install "vinta-django-billing[orgs]"

Then restore the pre-0.8 behaviour with two settings::

    VINTA_BILLING = {
        "BILLING_MANAGER_PREDICATE":
            "vinta_billing.contrib.orgs.any_member_may_manage_billing",
        "BILLING_RECIPIENTS": "vinta_billing.contrib.orgs.all_members",
    }

Every function here takes a *scope* and reads the organization out of it with
:func:`organization_pk_for`, which assumes the shipped ``BillingScope`` -- the
one whose generic key holds the payer's primary key. A project that swapped the
scope model for one with a real ``organization`` foreign key should copy these
four functions and read that field instead; they are short on purpose.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from vinta_billing.permissions import MANAGE_BILLING_PERMISSION


def organization_pk_for(scope: Any) -> Any | None:
    """The organization primary key a scope names, or ``None``.

    ``None`` for a scope that names something else -- a personal plan in a
    project that sells both -- and every predicate below reads that as "not an
    organization, so nobody manages it by membership".
    """
    if scope is None:
        return None
    object_id = getattr(scope, "object_id", None)
    return object_id or None


def any_member_may_manage_billing(user: Any, scope: Any) -> bool:
    """Any member of the scope's organization may manage its billing.

    The default before 0.8. The most permissive answer that is still
    tenant-safe -- it never lets one organization's member touch another's
    billing, but it draws no distinction between an owner and an ordinary
    member.

    Reads the membership model through ``vinta-django-orgs``, whose membership
    manager is deliberately unscoped -- membership is metadata *about* the
    tenancy, so scoping it to the selected organization would be circular.
    """
    from vinta_orgs.conf import get_organization_membership_model

    organization_pk = organization_pk_for(scope)
    if organization_pk is None or user is None or not user.is_authenticated:
        return False

    membership_model = get_organization_membership_model()
    return membership_model.objects.filter(user=user, organization_id=organization_pk).exists()


def member_holding_manage_billing(user: Any, scope: Any) -> bool:
    """A stricter predicate: the member must hold ``vinta_billing.manage_billing``.

    Offered rather than defaulted to. The permission is declared on
    ``Subscription`` but granted by nobody here, so making this the default
    would 403 every billing endpoint in a project that has not seeded a group
    carrying it.

    Asks ``vinta-django-orgs``' organization-scoped question, not
    ``user.has_perm``: the latter answers for whichever organization is *bound*,
    unions in the user's global permissions and groups, and says yes to every
    superuser. Billing is routinely read against a reseller **root** that is an
    ancestor of the bound organization, so all three would answer a question
    nobody asked.
    """
    from vinta_orgs.authorization import has_organization_permission
    from vinta_orgs.conf import get_organization_model

    organization_pk = organization_pk_for(scope)
    if organization_pk is None:
        return False
    organization = get_organization_model()._default_manager.filter(pk=organization_pk).first()
    if organization is None:
        return False
    return bool(has_organization_permission(user, MANAGE_BILLING_PERMISSION, organization))


def all_members(scope: Any) -> Sequence[Any]:
    """Every member of the scope's organization.

    The recipient default before 0.8. Errs towards telling too many people
    rather than too few -- a dunning message that reaches nobody ends in an
    unexplained suspension, which is a worse failure than one extra email.
    """
    from vinta_orgs.conf import get_organization_membership_model

    organization_pk = organization_pk_for(scope)
    if organization_pk is None:
        return []
    return list(
        get_organization_membership_model()
        .objects.filter(organization_id=organization_pk)
        .values_list("user_id", flat=True)
        .distinct()
    )


def members_holding_manage_billing(scope: Any) -> Sequence[Any]:
    """The members who hold ``vinta_billing.manage_billing`` in the organization.

    The counterpart to :func:`member_holding_manage_billing`, so that "who may
    change billing" and "who is told when it goes wrong" come from one grant
    rather than drifting apart. Offered rather than defaulted to, and for a
    sharper reason than the predicate: nothing here grants the permission, and a
    dunning ladder whose messages reach **nobody** ends in a suspension the
    payer was never warned about.

    Inactive memberships are excluded: a deactivated member is not somebody to
    tell, and ``holding_permission`` alone does not exclude them.
    """
    from vinta_orgs.conf import get_organization_membership_model

    organization_pk = organization_pk_for(scope)
    if organization_pk is None:
        return []
    return list(
        get_organization_membership_model()
        .objects.filter(organization_id=organization_pk)
        .active()
        .holding_permission(MANAGE_BILLING_PERMISSION)
        .values_list("user_id", flat=True)
        .distinct()
    )


def resolve_scope_from_organization(request: Any) -> Any | None:
    """``SCOPE_RESOLVER`` for a project whose middleware already resolves an org.

    Bridges ``vinta-django-orgs``' ``request.organization`` to the scope that
    names it, so a project keeps its existing tenancy and billing follows::

        VINTA_BILLING = {
            "SCOPE_RESOLVER": "vinta_billing.contrib.orgs.resolve_scope_from_organization",
        }

    Does not create a scope: resolving a request must not write. A project
    provisions the scope alongside the organization, with
    ``BillingScope.objects.get_or_create_for(organization)``.
    """
    from vinta_billing.conf import get_scope_model

    scope = getattr(request, "scope", None)
    if scope is not None:
        return scope

    organization = getattr(request, "organization", None)
    if organization is None:
        return None

    scope_for = getattr(get_scope_model()._default_manager, "scope_for", None)
    if scope_for is None:
        return None
    return scope_for(organization)
