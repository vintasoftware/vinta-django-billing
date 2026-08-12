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

from vinta_orgs.models import Organization


def all_members(organization: Organization) -> Sequence[Any]:
    """The default: every member of the organization.

    Errs towards telling too many people rather than too few -- a dunning
    message that reaches nobody ends in an unexplained suspension, which is a
    worse failure than one extra email.
    """
    from vinta_orgs.conf import get_organization_membership_model

    return list(
        get_organization_membership_model()
        .objects.filter(organization=organization)
        .values_list("user_id", flat=True)
        .distinct()
    )


def get_billing_recipients(organization: Organization) -> Sequence[Any]:
    """Run the configured resolver."""
    from billing.conf import get_object_from_setting

    return get_object_from_setting("BILLING_RECIPIENTS")(organization)
