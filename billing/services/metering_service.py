"""Records event occurrences as billable usage, exactly once, ever.

This is the highest-severity code in the billing system: occurrences of a recurring
series are computed in Postgres and never stored, so nothing exists to bill until
this service writes it — and a double-count here is silent revenue drift or an
overcharge, invisible until a customer disputes an invoice. There is no exception,
no failing test, no alert; just a wrong number.

Four properties carry that weight, in order of importance:

1. **The unique constraint is the mechanism.**
   ``MeteredOccurrence(organization, event_id, occurrence_start)`` plus
   ``bulk_create(..., ignore_conflicts=True)`` is what makes re-running a window,
   or running two overlapping windows, a no-op at the database level. The sweep
   window deliberately overlaps the previous one so a missed run self-heals;
   that is only safe because idempotence is enforced below the application, not
   remembered by it.
2. **The window bounds the expansion.** An open-ended weekly series is infinite;
   it contributes roughly four occurrences per monthly cycle because the meter
   only ever expands ``[window_start, window_end)`` and only keeps occurrences
   whose ``start_time`` falls inside it. Nothing is charged at series-creation
   time.
3. **Identity comes from the source, not from a second enumeration.**
   The project's :class:`~billing.metering.OccurrenceSource` is the one
   definition of which occurrences exist in a window. The meter never re-derives
   them from anything else: a second opinion about what happened would
   eventually disagree with the first, and the two would disagree about a
   customer's bill.

   Identity is ``(series root pk, occurrence start time)``. The series root half
   is durable — splits are normalised back to the original master. The start-time
   half is **not**: re-timing an occurrence creates a new identity and bills it
   again. See ``expand_occurrence_identities`` for why no durable alternative
   exists in the expansion today, and ``test_metering_reconciliation.py`` for the
   measured magnitude of the two identity-churn paths this leaves open. Both must
   be fixed before cycle close.
4. **Price is stamped at meter time.** ``is_within_allowance`` and ``unit_price``
   are resolved against the effective limit in force when the occurrence is
   recorded, so a later plan change or limit override cannot retroactively
   reprice usage that already happened.

``reconcile_period`` is the mitigation for the residual risk in all of the above:
it recomputes a closed cycle and reports drift both ways. It never writes.
"""

import datetime
import logging
from collections.abc import Iterable, Sequence
from decimal import Decimal

from django.db import transaction
from vinta_orgs.conf import get_organization_model

from billing.metering import get_occurrence_source
from billing.models import MeteredOccurrence, Subscription
from billing.services.billing_dataclasses import (
    EffectiveLimit,
    MeteringResult,
    OccurrenceIdentity,
    ReconciliationReport,
)
from billing.services.entitlement_service import EntitlementService, metered_resource_key
from billing.services.subscription_service import (
    billing_root_filter,
    resolve_billing_period_start,
    resolve_settlement_period,
)


logger = logging.getLogger(__name__)


#: Occurrences to expand per master per window. A window is hours wide, so this is
#: unreachable in practice for any sane series; it exists so a pathological rule
#: (``FREQ=SECONDLY``) cannot make one sweep allocate without bound.
MAX_OCCURRENCES_PER_MASTER = 10000

#: How many bulk-modification splits deep a series chain is followed before the walk
#: gives up. Each level is one query; a series split a hundred times is already
#: pathological, and the bound is what stops a cycle in mutable ``parent`` data from
#: hanging the sweep.
MAX_SERIES_CHAIN_DEPTH = 100

#: Price recorded for an occurrence that fell inside the included allowance, and
#: for one outside it when the plan carries no ``overage_unit_price``. Explicitly
#: zero rather than NULL: the column records what this occurrence was priced at,
#: and "nothing" is a price.
ZERO_PRICE = Decimal("0")


def _identity_sort_key(identity: OccurrenceIdentity) -> tuple[datetime.datetime, int, int]:
    """Stable, chronological ordering for the drift lists in a reconciliation report,
    so two runs over the same data produce byte-identical output."""
    return (identity.occurrence_start, identity.organization_id, identity.event_id)


class MeteringService:
    """Writes and audits ``MeteredOccurrence`` rows. Stateless; injected via DI."""

    def __init__(self, entitlement_service: EntitlementService | None = None) -> None:
        from billing.services.container import get_entitlement_service

        self._entitlement_service = entitlement_service or get_entitlement_service()

    # ------------------------------------------------------------------
    # Metering
    # ------------------------------------------------------------------

    def meter_occurrences_for_period(
        self,
        subscription: Subscription,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> MeteringResult:
        """Record every occurrence starting in ``[window_start, window_end)``.

        Safe to call repeatedly with the same window, and safe to call with a
        window overlapping one already swept — both are how a missed run heals.

        The whole call runs inside one transaction holding the billing root's
        subscription row lock (``EntitlementService.lock_billing_root``). The lock
        is not there for the inserts — the unique constraint already makes those
        idempotent — it is there for the **allowance stamping**, which reads how
        much of the included allowance the period has already consumed and then
        writes rows that depend on that count. Two concurrent sweeps without the
        lock would each read the same "0 used" and each stamp the first N
        occurrences as within-allowance, giving away the allowance twice.

        :param window_start: Inclusive lower bound on ``occurrence_start``.
        :param window_end: Exclusive upper bound. Callers sweeping live usage pass
            a value at or before "now"; passing a future bound meters occurrences
            that have not happened yet.
        """
        if window_end <= window_start:
            logger.warning(
                "Refusing to meter subscription %s: window end %s is not after window start %s.",
                subscription.pk,
                window_end,
                window_start,
            )
            return MeteringResult(
                subscription_id=subscription.pk,
                window_start=window_start,
                window_end=window_end,
                occurrences_seen=0,
                occurrences_recorded=0,
            )

        with transaction.atomic():
            self._entitlement_service.lock_billing_root(subscription.organization)
            identities = self.expand_occurrence_identities(subscription, window_start, window_end)
            recorded = self._record(subscription, identities)

        return MeteringResult(
            subscription_id=subscription.pk,
            window_start=window_start,
            window_end=window_end,
            occurrences_seen=len(identities),
            occurrences_recorded=recorded,
        )

    def _record(self, subscription: Subscription, identities: Sequence[OccurrenceIdentity]) -> int:
        """Insert the identities that are not already recorded; return rows gained.

        Already-recorded identities are filtered out *before* the allowance is
        assigned. That is not a substitute for the unique constraint — the
        ``ignore_conflicts=True`` below is still what guarantees idempotence, and
        it still fires on a genuine race. It is needed because the allowance is
        positional: without it, an occurrence recorded by an earlier overlapping
        window would consume an allowance slot here *as well*, pushing genuinely
        new occurrences into overage that should have been included.

        The row count is measured before and after rather than taken from
        ``bulk_create``'s return value, which cannot report which rows conflicted.
        """
        if not identities:
            return 0

        effective_limit = self._entitlement_service.get_effective_limit(
            subscription.organization, metered_resource_key()
        )
        already_recorded = self._existing_identities(subscription, identities)
        new_identities = sorted(
            (identity for identity in identities if identity not in already_recorded),
            key=lambda identity: (identity.occurrence_start, identity.event_id),
        )
        if not new_identities:
            return 0

        rows: list[MeteredOccurrence] = []
        # Allowance is consumed per billing period, so a window straddling a cycle
        # boundary starts the next cycle's allowance fresh — hence a dict keyed by
        # period rather than a single running counter.
        #
        # Within a period, allowance is consumed in **insertion order**, not
        # chronological order. New identities in this call are sorted by
        # occurrence_start, but rows an earlier sweep already recorded are counted,
        # not ranked, so an occurrence back-dated into an already-swept period lands
        # after everything recorded before it regardless of when it happened.
        # Acceptable today because every occurrence in a period carries the same
        # price, so ordering cannot change any row's `unit_price` — only which rows
        # sit inside the allowance, and the totals are identical either way.
        # Mid-period plan changes break that assumption: once two prices can apply
        # within one period, ordering decides which one a row gets, and this must
        # become a chronological rank. Re-ranking rows already stamped would
        # contradict "price is stamped at meter time and never repriced", so that is
        # a design decision for that work, not a local fix.
        consumed_by_period: dict[datetime.datetime, int] = {}
        for identity in new_identities:
            period_start = resolve_billing_period_start(subscription, identity.occurrence_start)
            if period_start not in consumed_by_period:
                consumed_by_period[period_start] = self._recorded_count_for_period(
                    subscription, period_start
                )
            position = consumed_by_period[period_start]
            consumed_by_period[period_start] = position + 1
            is_within_allowance, unit_price = self._price_for(effective_limit, position)
            rows.append(
                MeteredOccurrence(
                    organization_id=identity.organization_id,
                    subscription=subscription,
                    event_id=identity.event_id,
                    occurrence_start=identity.occurrence_start,
                    billing_period_start=period_start,
                    is_within_allowance=is_within_allowance,
                    unit_price=unit_price,
                )
            )

        before = self._recorded_total(subscription, consumed_by_period)
        MeteredOccurrence.objects.bulk_create(rows, ignore_conflicts=True)
        after = self._recorded_total(subscription, consumed_by_period)
        return after - before

    @staticmethod
    def _price_for(effective_limit: EffectiveLimit, position: int) -> tuple[bool, Decimal]:
        """Stamp allowance membership and price for the ``position``-th occurrence
        of a billing period (zero-based).

        ``limit_value is None`` is unlimited — the whole rollout runs there, since
        every organization sits on the ``unlimited`` plan — and everything is
        inside the allowance at no cost.
        """
        if effective_limit.limit_value is None or position < effective_limit.limit_value:
            return True, ZERO_PRICE
        return False, effective_limit.overage_unit_price or ZERO_PRICE

    @staticmethod
    def _recorded_count_for_period(
        subscription: Subscription, period_start: datetime.datetime
    ) -> int:
        return MeteredOccurrence.objects.for_billing_period(subscription.pk, period_start).count()

    @classmethod
    def _recorded_total(
        cls, subscription: Subscription, period_starts: Iterable[datetime.datetime]
    ) -> int:
        return sum(
            cls._recorded_count_for_period(subscription, period_start)
            for period_start in period_starts
        )

    @staticmethod
    def _existing_identities(
        subscription: Subscription, identities: Sequence[OccurrenceIdentity]
    ) -> set[OccurrenceIdentity]:
        """Which of ``identities`` the ledger already holds.

        Queried by the unique-constraint tuple itself so this cannot disagree with
        what an insert would conflict on. Scoped by ``occurrence_start`` range and
        the pooled organization ids rather than by an ``OR`` over every tuple, so
        the query stays one indexable predicate regardless of window size.

        **Deliberately not filtered by ``subscription``.** The constraint is
        ``(organization, event_id, occurrence_start)`` — no subscription column — so
        narrowing here by ``subscription_id`` would make this pre-filter *stricter*
        than the thing it is predicting. A row recorded under a different
        subscription for the same organization would then be invisible here but
        still conflict on insert: the occurrence would silently consume an allowance
        position without producing a row, pushing a genuinely new occurrence into
        overage while the organization is under its ceiling. An overcharge that
        ``reconcile_period`` reports as ``drift == 0``, because the identity sets
        still agree.

        That state is reachable without any data corruption: an organization that
        was its own billing root (and so had its own ``Subscription``, and rows
        stamped with it) can be re-parented under a reseller and demoted, after
        which the ancestor's sweep meters its events under the *ancestor's*
        subscription. Adding ``subscription`` to the constraint instead was the
        alternative; it was rejected because it would make the same occurrence
        billable once per subscription that ever pooled it, which is the double-bill
        this table exists to prevent.

        ``subscription`` is still taken as an argument: it is what the caller stamps
        onto new rows, and keeping the signature honest about that is cheaper than a
        reader wondering why it was dropped.
        """
        organization_ids = {identity.organization_id for identity in identities}
        starts = [identity.occurrence_start for identity in identities]
        existing = MeteredOccurrence.objects.for_organizations(sorted(organization_ids)).filter(
            occurrence_start__gte=min(starts),
            occurrence_start__lte=max(starts),
        )
        return {
            OccurrenceIdentity(
                organization_id=organization_id,
                event_id=event_id,
                occurrence_start=occurrence_start,
            )
            for organization_id, event_id, occurrence_start in existing.values_list(
                "organization_id", "event_id", "occurrence_start"
            )
        }

    # ------------------------------------------------------------------
    # Expansion
    # ------------------------------------------------------------------

    def expand_occurrence_identities(
        self,
        subscription: Subscription,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> list[OccurrenceIdentity]:
        """Every billable occurrence in the window, as identity tuples.

        The occurrences themselves come from the project's configured
        :class:`~billing.metering.OccurrenceSource`. What a "billable occurrence"
        is -- one appointment, one expansion of a recurring series, one API call
        -- is entirely the project's definition; this method only turns whatever
        it reports into the ledger's identity tuple and drops anything outside
        the window.

        The result is deduplicated on the identity tuple. That is belt-and-braces
        -- a source is expected not to report the same occurrence twice -- but the
        alternative is relying on ``ON CONFLICT DO NOTHING`` to absorb duplicates
        *within a single statement*, and this way ``occurrences_seen`` counts
        distinct occurrences rather than expansion outputs.

        Occurrences naming an organization outside the pooled subtree are
        dropped rather than billed to the wrong root: the source is handed the
        pool, so reporting outside it is a bug in the source, and silently
        charging another tenant for it would be worse than under-counting.
        """
        organization_ids = self._entitlement_service.get_pooled_organization_ids(
            subscription.organization
        )
        pool = set(organization_ids)

        identities: dict[OccurrenceIdentity, None] = {}
        for occurrence in get_occurrence_source().iter_occurrences(
            organization_ids, window_start, window_end
        ):
            if not window_start <= occurrence.occurred_at < window_end:
                continue
            if occurrence.organization_id not in pool:
                logger.warning(
                    "Occurrence source reported organization %s, which is outside "
                    "the pooled subtree of subscription %s; skipping.",
                    occurrence.organization_id,
                    subscription.pk,
                )
                continue
            identities[
                OccurrenceIdentity(
                    organization_id=occurrence.organization_id,
                    event_id=occurrence.external_id,
                    occurrence_start=occurrence.occurred_at,
                )
            ] = None
        return list(identities)

    def reconcile_period(
        self, subscription: Subscription, period: datetime.datetime
    ) -> ReconciliationReport:
        """Recompute a billing cycle and report drift against what was metered.

        ``period`` is any moment inside the cycle of interest; the cycle's exact
        bounds come from ``resolve_settlement_period`` — the monthly settlement
        window cycle close actually rolls and settles. For a monthly plan (and for
        every current-period reconcile, which never steps) this is identical to what
        the meter stamped ``billing_period_start`` with; for an annually-billed plan
        it walks the subscription's history back one month at a time rather than by
        twelve-month strides, matching how those historical rows were stamped and how
        close rolls.

        Read-only by design. A repair that ran automatically would hide the
        condition it was repairing, and the two drift directions do not have the
        same remedy: ``unmetered`` rows are usage that was never billed (re-run
        the sweep), while ``orphaned`` rows may be perfectly correct — an event
        deleted after its occurrences happened leaves rows behind on purpose,
        because an occurrence that was billed stays billed.

        **Scope: identity only. Pricing is explicitly out of scope.** This compares
        *which* occurrences exist, never ``is_within_allowance`` or ``unit_price`` —
        the two columns that actually produce an invoice line. A stamping defect
        that priced every row wrong while recording the right set of occurrences
        reports ``drift == 0, is_clean == True``. Anyone treating a clean report as
        "this invoice is correct" is mistaken; it means "this invoice bills the
        right occurrences".

        This is a gap, not an oversight, and recomputing prices here would be worse
        than not doing it. Allowance position is consumed in insertion order (see
        ``_record``), which is a function of *when the sweeps ran*, not of what the
        source reports now — so a recompute has no reproducible expected value to
        compare against and would report false drift on correctly-priced rows.
        Making prices reconcilable requires first making allowance ranking
        deterministic (chronological), which is the same change mid-period plan
        changes force. This waits for cycle close, the first point anything reads
        these columns to produce money.
        """
        period_start, period_end = resolve_settlement_period(subscription, period)
        expected = set(self.expand_occurrence_identities(subscription, period_start, period_end))
        metered = {
            OccurrenceIdentity(
                organization_id=organization_id,
                event_id=event_id,
                occurrence_start=occurrence_start,
            )
            for (
                organization_id,
                event_id,
                occurrence_start,
            ) in MeteredOccurrence.objects.for_billing_period(
                subscription.pk, period_start
            ).values_list("organization_id", "event_id", "occurrence_start")
        }
        return ReconciliationReport(
            subscription_id=subscription.pk,
            billing_period_start=period_start,
            billing_period_end=period_end,
            expected_count=len(expected),
            metered_count=len(metered),
            unmetered=tuple(sorted(expected - metered, key=_identity_sort_key)),
            orphaned=tuple(sorted(metered - expected, key=_identity_sort_key)),
        )

    # ------------------------------------------------------------------
    # Sweep helpers
    # ------------------------------------------------------------------

    @staticmethod
    def subscriptions_to_sweep() -> Iterable[int]:
        """Ids of every subscription the periodic sweep should meter.

        Subscriptions **whose organization is currently a billing root**, not every
        ``Subscription`` row. The two are supposed to be the same set —
        ``SubscriptionService.create_subscription_for_organization`` skips reseller
        children — but that is an invariant nothing enforces at the database level,
        and it is broken by an ordinary admin action: re-parenting an organization
        under a reseller, or clearing ``can_invite_organizations``, demotes a root
        while leaving its ``Subscription`` behind.

        Sweeping a demoted root is not merely redundant work.
        ``expand_occurrence_identities`` pools *its billing root's* whole subtree —
        which after demotion is the ancestor's — so the demoted subscription would
        meter the ancestor's entire subtree a second time, under its own
        subscription id, corrupting both subscriptions' allowance positions.
        Filtering here is what stops a tenancy edit from becoming a billing
        incident.

        Exclusions are logged rather than silently dropped: a non-empty exclusion
        list means the invariant is violated and somebody should reconcile that
        organization's ledger, which is invisible if the sweep just skips it.
        """
        all_ids = set(Subscription.objects.values_list("pk", flat=True))
        root_ids = set(
            Subscription.objects.filter(
                # Through a subquery on Organization rather than a `organization__`
                # -prefixed copy of the predicate, so `billing_root_filter` stays the
                # single definition of "is a billing root" (see `is_billing_root`).
                organization__in=get_organization_model().objects.filter(billing_root_filter())
            ).values_list("pk", flat=True)
        )
        excluded = all_ids - root_ids
        if excluded:
            logger.warning(
                "Excluding %s subscription(s) from the metering sweep: their organizations are "
                "no longer billing roots, so their usage pools against an ancestor. Ids: %s. "
                "Their existing ledger rows are untouched and may need reconciling.",
                len(excluded),
                sorted(excluded),
            )
        return sorted(root_ids)
