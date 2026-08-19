"""What the test project bills for.

This is the file a host application writes. Everything the engine knows about
widgets, seats and events enters through here.

Every counter reads through ``original_manager`` -- the *unscoped* manager
``vinta-django-orgs`` puts on scoped models -- and never ``objects``. Usage
pools across a whole billing subtree, so a counter is asked about several
organizations at once and must not be narrowed to whichever one happens to be
bound to the current context. In a Celery beat run there is no bound
organization at all, and the scoped manager would report zero usage for
everybody: every ceiling would silently read as empty.
"""

from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from vinta_billing.constants import LimitKind, LimitRemedy
from vinta_billing.counting import UsageContext, count_by_organization, merge_breakdowns
from vinta_billing.registry import entitlements, resources
from vinta_billing.services.entitlement_service import count_metered_occurrences


#: Read by the seat counter out of ``UsageContext.extra``. Accepting an
#: invitation is net zero on seats -- the pending row becomes the membership it
#: was already holding a seat for -- so the invitation being accepted has to be
#: excluded, or the accept fails its own check at exactly the ceiling and an
#: organization can never fill its last seat.
EXCLUDE_INVITATION_ID = "exclude_invitation_id"


def count_widgets(context: UsageContext) -> dict[int, int]:
    from tests.testapp.models import Widget

    return count_by_organization(
        Widget.original_manager.filter(organization_id__in=context.organization_ids)
    )


def count_seats(context: UsageContext) -> dict[int, int]:
    """Active seats plus still-open invitations, per organization.

    The two tables are grouped separately and merged key-wise rather than
    concatenated, so an organization holding both kinds of seat is not
    double-keyed in the result.
    """
    from tests.testapp.models import Seat, SeatInvitation

    seats = count_by_organization(
        Seat.original_manager.filter(organization_id__in=context.organization_ids, is_active=True)
    )
    pending = SeatInvitation.original_manager.filter(
        organization_id__in=context.organization_ids, accepted_at__isnull=True
    )
    exclude_id = context.get(EXCLUDE_INVITATION_ID)
    if exclude_id is not None:
        pending = pending.filter(~Q(pk=exclude_id))
    return merge_breakdowns(seats, count_by_organization(pending))


def register() -> None:
    """Called from ``TestAppConfig.ready()``."""
    resources.register(
        "widgets",
        label=_("Widgets"),
        kind=LimitKind.PREPAID,
        counter=count_widgets,
        remedy=LimitRemedy.UPGRADE_PLAN,
    )
    resources.register(
        "seats",
        label=_("Seats"),
        kind=LimitKind.PREPAID,
        counter=count_seats,
        remedy=LimitRemedy.PURCHASE_ADD_ON,
        # The one key `count_seats` reads. Declaring it turns on the check that
        # refuses the same key when it is aimed at a resource whose counter would
        # quietly ignore it. `widgets` above is deliberately left undeclared, so
        # the suite covers both a 0.3.0-style registration and an opted-in one.
        usage_extra_keys={EXCLUDE_INVITATION_ID},
    )
    resources.register(
        "event_occurrences",
        label=_("Event occurrences"),
        kind=LimitKind.POSTPAID,
        counter=count_metered_occurrences,
        remedy=LimitRemedy.ADD_PAYMENT_METHOD,
        # `count_metered_occurrences` reads nothing out of `UsageContext.extra`,
        # and says so: an empty declaration is what makes a seat exclusion aimed
        # at this resource raise instead of being silently dropped.
        usage_extra_keys=frozenset(),
    )

    entitlements.register("white_label", label=_("White-label branding"))
    entitlements.register("advanced_reporting", label=_("Advanced reporting"))
