"""Where metered occurrences come from, and how they are described.

``MeteredOccurrence`` is this package's ledger: one row per billable thing that
happened, stamped with the billing period it falls in. What produced the thing
is the project's business -- an appointment, an API call, a rendered video --
and the ledger holds only a soft reference to it (``event_id``, a plain integer,
deliberately not a foreign key, so this package's migrations never point at a
host table).

Two jobs the project owns:

* **Producing** occurrences for a window, so the meter has something to write.
* **Describing** them, so the usage ledger endpoint can render more than a bare
  id.

Both live behind one object, configured as ``OCCURRENCE_SOURCE``. Left unset,
metering is a no-op and the ledger renders ids without detail -- which is the
correct behaviour for a project that only caps prepaid resources.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Occurrence:
    """One billable thing that happened.

    ``external_id`` and ``occurred_at`` together are what makes metering
    idempotent: the meter upserts on them, so re-running over the same window
    writes nothing the second time. A source that invents a fresh id per run
    breaks that and double-bills on every retry.
    """

    external_id: int
    organization_id: int
    occurred_at: datetime.datetime
    quantity: int = 1
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class OccurrenceSource(Protocol):
    """The project's answer to "what happened, and what was it?"."""

    def iter_occurrences(
        self,
        organization_ids: Sequence[int],
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> Iterable[Occurrence]:
        """Every billable occurrence in ``[window_start, window_end)``.

        Must be deterministic for a given window: the meter re-runs windows on
        retry and during reconciliation, and compares what it finds against what
        it already wrote.
        """
        ...

    def describe(self, external_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        """Detail for the ledger endpoint, keyed by ``external_id``.

        Batched -- one call per page, never per row. Ids the project can no
        longer resolve (the underlying object was deleted) are simply absent
        from the mapping, and render as ``null``.
        """
        ...


class NullOccurrenceSource:
    """The default: nothing is metered, and nothing has detail."""

    def iter_occurrences(
        self,
        organization_ids: Sequence[int],
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> Iterable[Occurrence]:
        return ()

    def describe(self, external_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        return {}


def get_occurrence_source() -> OccurrenceSource:
    """The configured source, or the null one when the project set none."""
    from billing.conf import get_object_from_setting

    source = get_object_from_setting("OCCURRENCE_SOURCE")
    if source is None:
        return NullOccurrenceSource()
    if isinstance(source, type):
        return source()  # type: ignore[no-any-return]
    return source  # type: ignore[no-any-return]
