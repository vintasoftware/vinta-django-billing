"""The recurring work this package needs run, as plain callables.

**No Celery.** These are ordinary functions, so how they are scheduled is
entirely the project's business -- Celery beat, a cron entry, a management
command, RQ, a Lambda on a timer. Nothing here imports a queue.

There are two shapes. A *sweep* (``process_dunning``, ``meter_event_occurrences``,
``close_billing_periods``, ``check_approaching_limits``) finds the subscriptions
that need work and hands each one to a *per-subscription job*. The sweeps do not
decide how that hand-off happens: they call a ``dispatch`` callable, which
defaults to running the job inline.

Wiring it to Celery is about fifteen lines in the project:

    # myproject/billing_tasks.py
    from celery import shared_task
    from vinta_billing import jobs

    @shared_task
    def process_dunning_for_subscription(subscription_id):
        jobs.process_dunning_for_subscription(subscription_id)

    @shared_task
    def process_dunning():
        jobs.process_dunning(
            dispatch=lambda job, *args: process_dunning_for_subscription.delay(*args)
        )

The default inline dispatcher is correct for a cron-driven project and for
tests; it is the wrong choice for a large installation, where one slow
subscription would hold up the whole sweep. Configure a queueing dispatcher
through ``VINTA_BILLING['JOB_DISPATCHER']``, or pass one per call.

Every per-subscription job is idempotent, because at-least-once delivery is the
only guarantee a queue gives: the same arguments produce the same rows, and a
redelivery writes nothing new.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from typing import Any

from django.utils import timezone

from vinta_billing.constants import BillingState
from vinta_billing.models import Subscription
from vinta_billing.services.container import (
    get_cycle_close_service,
    get_dunning_service,
    get_metering_service,
    get_usage_warning_service,
)
from vinta_billing.services.cycle_close_service import CycleCloseService
from vinta_billing.services.dunning_service import DunningService
from vinta_billing.services.metering_service import MeteringService
from vinta_billing.services.usage_warning_service import UsageWarningService


#: ``(job, *args) -> None``. Called once per subscription a sweep finds.
Dispatch = Callable[..., Any]


def run_inline(job: Dispatch, *args: Any) -> None:
    """The default dispatcher: call the job straight away, in this process.

    Correct for cron-driven projects and for tests. Wrong for a large
    installation -- one slow subscription holds up every subscription behind it,
    and a crash mid-sweep loses the rest of the run. Configure
    ``JOB_DISPATCHER`` to hand each job to a queue instead.
    """
    job(*args)


def get_dispatcher() -> Dispatch:
    """The configured dispatcher, or :func:`run_inline`."""
    from vinta_billing.conf import get_object_from_setting

    dispatcher = get_object_from_setting("JOB_DISPATCHER")
    if dispatcher is None:
        return run_inline
    if isinstance(dispatcher, type):
        return dispatcher()
    return dispatcher


logger = logging.getLogger(__name__)


#: How far back each sweep re-reads. Deliberately **wider than the beat interval**
#: (a sweep every 15 minutes is the shape this was sized against), so
#: consecutive runs overlap heavily: at six hours, up to 23
#: consecutive missed runs — a worker outage, a redeploy, a broker incident — are
#: made up for by the next successful run with no operator action and no backfill
#: command. Re-reading an already-metered stretch costs one expansion query and
#: inserts nothing, because ``MeteredOccurrence``'s unique constraint absorbs it.
#:
#: Widening this is cheap and safe; narrowing it below the beat interval would
#: leave gaps that are silently never billed.
#:
#: **Operator action after an outage longer than this.** Self-healing stops at six
#: hours; beyond that the un-swept stretch is never billed, and nothing raises,
#: because the next sweep only ever looks six hours back. There is no backfill
#: management command. Re-meter the gap by calling
#: ``MeteringService.meter_occurrences_for_period(subscription, gap_start, gap_end)``
#: for each subscription in ``MeteringService.subscriptions_to_sweep()`` — it is
#: idempotent, so an over-wide window is safe — then confirm with
#: ``reconcile_period``, which reports the recovered stretch as ``unmetered``
#: before the backfill and clean after it.
METERING_SWEEP_WINDOW = datetime.timedelta(hours=6)


def meter_event_occurrences(dispatch: Dispatch | None = None) -> None:
    """Beat entry point: fan out a metering sweep for every subscription.

    The window is computed **once here** and passed explicitly to each
    per-subscription task, rather than being recomputed inside them. A task that
    derived its own window from ``timezone.now()`` would sweep a different stretch
    on every at-least-once delivery redelivery, so a retry would not be a repeat
    of the same work — which is exactly the property that makes redelivery safe.
    """

    dispatch = dispatch or get_dispatcher()
    window_end = timezone.now()
    window_start = window_end - METERING_SWEEP_WINDOW
    for subscription_id in MeteringService.subscriptions_to_sweep():
        dispatch(
            meter_subscription_event_occurrences,
            subscription_id,
            window_start.isoformat(),
            window_end.isoformat(),
        )


def meter_subscription_event_occurrences(
    subscription_id: int,
    window_start: str,
    window_end: str,
    metering_service: MeteringService | None = None,
) -> None:
    """Meter one subscription's pooled subtree over an explicit window.

    Idempotent, as at-least-once delivery requires: the same arguments produce
    the same rows, and re-running inserts nothing.

    A subscription deleted between fan-out and execution is logged and skipped
    rather than raising — a raising task is redelivered and fails identically
    forever, turning a benign race into a permanent stream of alerts.
    """
    metering_service = metering_service or get_metering_service()
    subscription = Subscription.objects.filter(pk=subscription_id).first()
    if subscription is None:
        logger.info(
            "Skipping occurrence metering for subscription %s: it no longer exists.",
            subscription_id,
        )
        return

    result = metering_service.meter_occurrences_for_period(
        subscription,
        datetime.datetime.fromisoformat(window_start),
        datetime.datetime.fromisoformat(window_end),
    )
    logger.info(
        "Metered subscription %s over [%s, %s): %s occurrences seen, %s newly recorded.",
        result.subscription_id,
        result.window_start,
        result.window_end,
        result.occurrences_seen,
        result.occurrences_recorded,
    )


def process_dunning(dispatch: Dispatch | None = None) -> None:
    """Beat entry point: fan out one dunning tick per subscription currently
    GRACE or RESTRICTED.

    Subscriptions on any other ``billing_state`` (``ACTIVE``, ``FREE``,
    ``CANCELLED``) are never selected -- once a subscription leaves GRACE for
    ACTIVE (a successful retry, confirmed through the subscription-payment
    webhook), the next run of this query no longer includes it, which is what
    stops the ladder from retrying an already-resolved subscription.
    """

    dispatch = dispatch or get_dispatcher()
    subscription_ids = list(
        Subscription.objects.filter(
            billing_state__in=(BillingState.GRACE, BillingState.RESTRICTED)
        ).values_list("pk", flat=True)
    )
    for subscription_id in subscription_ids:
        dispatch(process_dunning_for_subscription, subscription_id)


def process_dunning_for_subscription(
    subscription_id: int,
    dunning_service: DunningService | None = None,
) -> None:
    """One dunning tick for one subscription, dispatched through
    ``DunningService.process_subscription`` -- never a direct
    ``billing_state`` write here (see ``vinta_billing.services.billing_state_machine``).

    Idempotent under at-least-once delivery redelivery:
    ``DunningService``'s own retry-bucket gate (``Subscription.last_dunning_attempt_at``)
    and the retry charge's bucket-derived ``idempotency_key`` -- both views of
    one ``retry_attempt_ordinal`` -- are what make a redelivered tick harmless,
    not anything here.

    A subscription deleted between fan-out and execution is logged and skipped
    rather than raising -- a raising task is redelivered and fails identically
    forever, turning a benign race into a permanent stream of alerts (same
    reasoning as ``meter_subscription_event_occurrences``, above).

    ``dunning_service.process_subscription`` itself is wrapped in the same
    best-effort ``except Exception`` guard ``close_subscription_billing_period``
    (below) already carries, for the same reason: a provider fault the
    adapter layer does not translate into a typed, expected outcome (a
    genuine Stripe integration/transport error, or a translated error type this
    call site does not yet know to catch) must not be allowed to raise out of
    this job. An
    uncaught raise here is not a one-off failure -- per this task's own
    docstring above, it is redelivered and fails identically forever, so one
    subscription's provider fault would silently stop that subscription's
    entire ladder (no further retry, no reminder, no final-warning email)
    while looking, from the outside, like nothing is wrong.
    """
    dunning_service = dunning_service or get_dunning_service()
    subscription = Subscription.objects.filter(pk=subscription_id).first()
    if subscription is None:
        logger.info(
            "Skipping dunning tick for subscription %s: it no longer exists.",
            subscription_id,
        )
        return
    try:
        dunning_service.process_subscription(subscription)
    except Exception:
        logger.exception(
            "Dunning tick failed for subscription %s; the ladder's own bookkeeping "
            "(last_dunning_attempt_at, the retry throttle bucket) is unaffected, so "
            "the next beat tick retries.",
            subscription_id,
        )


def close_billing_periods(dispatch: Dispatch | None = None) -> None:
    """Beat entry point: fan out one cycle-close per subscription whose current
    billing period has ended.

    The window (which subscriptions are due) is decided **once here** from
    ``timezone.now()`` and each subscription is closed in its own task, so one
    subscription's close failing (a declined overage charge, a provider error)
    records its failure and does not abort the rest of the sweep — a
    best-effort-across-subscriptions approach. Each close is idempotent (the rolled
    ``current_period_start`` is the durable marker; the overage charge carries a
    ``(subscription, period_start)`` idempotency key), so a
    at-least-once delivery redelivery is harmless.

    Only billing-root subscriptions with an elapsed period are selected
    (``CycleCloseService.subscriptions_to_close`` reuses
    ``MeteringService.subscriptions_to_sweep`` so "which subscription owns this
    usage" has a single definition).
    """

    dispatch = dispatch or get_dispatcher()
    for subscription_id in CycleCloseService.subscriptions_to_close():
        dispatch(close_subscription_billing_period, subscription_id)


def close_subscription_billing_period(
    subscription_id: int,
    cycle_close_service: CycleCloseService | None = None,
) -> None:
    """Close every elapsed period for one subscription, dispatched through
    ``CycleCloseService.close_subscription`` — the single place a period is
    settled and rolled (see that method's docstring).

    A subscription deleted between fan-out and execution is logged and skipped
    rather than raising (same reasoning as ``meter_subscription_event_occurrences``).

    A close failure (declined charge, provider error) is caught and logged rather
    than re-raised: the period stays unrolled, so the next beat tick re-dispatches
    and retries it (with the same overage idempotency key, so a partially-charged
    period does not double-charge), and one poison subscription never spins the
    task or blocks the rest of the sweep.
    """
    cycle_close_service = cycle_close_service or get_cycle_close_service()
    subscription = Subscription.objects.filter(pk=subscription_id).first()
    if subscription is None:
        logger.info(
            "Skipping cycle close for subscription %s: it no longer exists.",
            subscription_id,
        )
        return
    try:
        closed = cycle_close_service.close_subscription(subscription)
    except Exception:
        logger.exception(
            "Cycle close failed for subscription %s; the period is left unrolled and will be "
            "retried on the next sweep (the overage idempotency key prevents a double charge).",
            subscription_id,
        )
        return
    logger.info(
        "Cycle close for subscription %s settled %s period(s).",
        subscription_id,
        len(closed),
    )


def check_approaching_limits(dispatch: Dispatch | None = None) -> None:
    """Beat entry point: fan out one approaching-limit check per subscription
    that could still be warned before being blocked.

    Excludes ``RESTRICTED`` (already blocked -- see
    ``UsageWarningService.check_subscription`` for why warning it further adds
    nothing) and ``CANCELLED`` (running out the clock to ``FREE``, not
    accruing toward a block). ``FREE``, ``ACTIVE``, and ``GRACE`` subscriptions
    are all in scope -- a free-tier organization approaching its seat limit
    needs the same proactive warning as a paid one.
    """

    dispatch = dispatch or get_dispatcher()
    subscription_ids = list(
        Subscription.objects.exclude(
            billing_state__in=(BillingState.RESTRICTED, BillingState.CANCELLED)
        ).values_list("pk", flat=True)
    )
    for subscription_id in subscription_ids:
        dispatch(check_approaching_limits_for_subscription, subscription_id)


def check_approaching_limits_for_subscription(
    subscription_id: int,
    usage_warning_service: UsageWarningService | None = None,
) -> None:
    """One approaching-limit sweep for one subscription, dispatched through
    ``UsageWarningService.check_subscription`` -- the single place "approaching
    a limit" is defined (see that method's docstring).

    Idempotent under at-least-once delivery redelivery and safe to re-run on
    every beat tick: ``LimitWarningNotification``'s unique constraint, not
    anything here, is what keeps a still-crossed threshold from re-notifying
    every tick within the same billing cycle.

    A subscription deleted between fan-out and execution is logged and skipped
    rather than raising -- a raising task is redelivered and fails identically
    forever, turning a benign race into a permanent stream of alerts (same
    reasoning as ``meter_subscription_event_occurrences``/
    ``process_dunning_for_subscription``, above).
    """
    usage_warning_service = usage_warning_service or get_usage_warning_service()
    subscription = Subscription.objects.filter(pk=subscription_id).first()
    if subscription is None:
        logger.info(
            "Skipping approaching-limit check for subscription %s: it no longer exists.",
            subscription_id,
        )
        return
    usage_warning_service.check_subscription(subscription)
