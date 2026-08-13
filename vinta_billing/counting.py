"""What a usage counter is handed, and the plumbing every counter wants.

A project's counters are ordinary functions taking a :class:`UsageContext` and
returning ``{organization_id: count}``. Almost all of them are one queryset and
a ``GROUP BY``, which is what :func:`count_by_organization` does; the two-table
ones merge with :func:`merge_breakdowns`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.db.models import Count
from django.db.models.query import QuerySet


if TYPE_CHECKING:
    from vinta_billing.models import Subscription


@dataclass(frozen=True)
class UsageContext:
    """Everything a usage counter is allowed to depend on.

    A single parameter object rather than a widening positional signature: most
    counters need only ``organization_ids``, and the ones that need more should
    not force every other counter to grow a parameter it ignores.
    """

    organization_ids: Sequence[int]
    subscription: Subscription | None = None
    extra: dict[str, Any] | None = None
    """Per-call data a project's own counters agree on with their call sites.

    The engine never reads this. It exists so a project can thread something
    like "the invitation currently being accepted, which must not be
    double-counted" through :meth:`EntitlementService.check_limit` without this
    library needing a concept of invitations.
    """

    def get(self, key: str, default: Any = None) -> Any:
        """Read one key out of ``extra``, tolerating ``extra=None``."""
        if self.extra is None:
            return default
        return self.extra.get(key, default)


def count_by_organization(queryset: QuerySet[Any]) -> dict[int, int]:
    """Turn any organization-scoped queryset into ``{organization_id: row_count}``.

    Organizations with no matching rows are *absent* from the result rather than
    present with a zero: ``GROUP BY`` never emits a row for them, which is
    exactly the contract a usage counter promises, so no caller has to remember
    to strip zero entries.
    """
    # The leading `.order_by()` clears any ordering the caller's queryset may
    # carry. It is load-bearing, not decoration: Django appends `ORDER BY`
    # columns to `GROUP BY` too, so an ordered queryset would split one
    # organization's rows into several groups keyed by whatever else it ordered
    # on, and the comprehension below would silently keep only the last one --
    # under-reporting usage, which in a billing engine means under-charging.
    # A `Meta.ordering` on the counted model is enough to trigger it, so this
    # protects counters whose author never wrote an `order_by` at all.
    #
    # `Count("pk")` rather than `Count("pk", distinct=True)`: no chain feeding
    # this has a row-multiplying join, so distinct is unnecessary, and on a
    # model with a composite primary key Django raises
    # `ValueError("COUNT(DISTINCT) doesn't support composite primary keys")`.
    return {
        row["organization_id"]: row["usage_count"]
        for row in queryset.order_by().values("organization_id").annotate(usage_count=Count("pk"))
    }


def merge_breakdowns(*breakdowns: dict[int, int]) -> dict[int, int]:
    """Sum any number of ``{organization_id: count}`` maps key-wise into one.

    For a counter whose "one unit of usage" spans more than one table, the same
    organization can legitimately appear in both source breakdowns, so the maps
    must be added together rather than merged by last-write-wins.
    """
    merged: dict[int, int] = {}
    for breakdown in breakdowns:
        for organization_id, count in breakdown.items():
            merged[organization_id] = merged.get(organization_id, 0) + count
    return merged
