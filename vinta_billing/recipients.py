"""Who hears about billing.

A failed charge and an approaching limit both need somebody to tell. Which
members of an organization those are is a project's decision -- it depends on a
role or a flag that ``vinta-django-orgs``' membership model does not have -- so
it comes in through ``BILLING_RECIPIENTS``.

    VINTA_BILLING = {"BILLING_RECIPIENTS": "myproject.billing.owners_and_admins"}

    def owners_and_admins(organization):
        return list(
            Membership.objects.filter(organization=organization)
            .filter(Q(role="admin") | Q(is_billing_owner=True))
            .values_list("user_id", flat=True)
            .distinct()
        )
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from vinta_orgs.conf import get_organization_membership_model
from vinta_orgs.models import AbstractOrganization

from vinta_billing.conf import get_object_from_setting
from vinta_billing.permissions import MANAGE_BILLING_PERMISSION


def all_members(organization: AbstractOrganization) -> Sequence[Any]:
    """The default: every member of the organization.

    Errs towards telling too many people rather than too few -- a dunning
    message that reaches nobody ends in an unexplained suspension, which is a
    worse failure than one extra email.
    """
    return list(
        get_organization_membership_model()
        .objects.filter(organization_id=organization.pk)
        .values_list("user_id", flat=True)
        .distinct()
    )


def members_holding_manage_billing(organization: AbstractOrganization) -> Sequence[Any]:
    """The members who hold ``vinta_billing.manage_billing`` in the organization.

    The counterpart to
    :func:`vinta_billing.permissions.member_holding_manage_billing`, so that "who
    may change billing" and "who is told when it goes wrong" come from one grant
    rather than drifting apart. Offered rather than defaulted to, and for a
    sharper reason than the predicate: nothing here grants the permission, and a
    dunning ladder whose messages reach **nobody** ends in a suspension the payer
    was never warned about. Point ``BILLING_RECIPIENTS`` at it only once the
    grant exists::

        VINTA_BILLING = {
            "BILLING_RECIPIENTS": (
                "vinta_billing.recipients.members_holding_manage_billing"
            ),
        }

    Inactive memberships are excluded: a deactivated member is not somebody to
    tell, and ``holding_permission`` alone does not exclude them.
    """
    return list(
        get_organization_membership_model()
        .objects.filter(organization_id=organization.pk)
        .active()
        .holding_permission(MANAGE_BILLING_PERMISSION)
        .values_list("user_id", flat=True)
        .distinct()
    )


def get_billing_recipients(organization: AbstractOrganization) -> Sequence[Any]:
    """Run the configured resolver."""
    return get_object_from_setting("BILLING_RECIPIENTS")(organization)
