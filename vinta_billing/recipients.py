"""Who hears about billing.

A failed charge and an approaching limit both need somebody to tell. Who that is
depends on a role or a flag no scope model can be assumed to have, so it comes
in through ``BILLING_RECIPIENTS``.

    VINTA_BILLING = {"BILLING_RECIPIENTS": "myproject.billing.owners_and_admins"}

    def owners_and_admins(scope):
        return list(
            Membership.objects.filter(organization_id=scope.object_id)
            .filter(Q(role="admin") | Q(is_billing_owner=True))
            .values_list("user_id", flat=True)
            .distinct()
        )
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from vinta_billing.conf import get_object_from_setting
from vinta_billing.models import AbstractBillingScope


def scope_owner(scope: AbstractBillingScope) -> Sequence[Any]:
    """The default: the scope's owner, if it has one.

    The counterpart to
    :func:`~vinta_billing.permissions.owner_may_manage_billing`, so that "who
    may change billing" and "who is told when it goes wrong" come from one
    column rather than drifting apart.

    Returns an empty list for a scope with no owner, and that is the one thing
    worth knowing about this default: a dunning ladder whose messages reach
    nobody ends in a suspension the payer was never warned about. A project
    billing organizations should either populate ``scope.owner`` with the
    billing contact or configure ``BILLING_RECIPIENTS`` -- and
    ``LoggingNotifier``, the shipped default notifier, logs what it would have
    sent, so an empty recipient list is at least visible in a log rather than
    silent.
    """
    return [scope.owner_id] if scope.owner_id is not None else []


def get_billing_recipients(scope: AbstractBillingScope) -> Sequence[Any]:
    """Run the configured resolver."""
    return get_object_from_setting("BILLING_RECIPIENTS")(scope)
