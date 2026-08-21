"""Two sweeps closing the same subscription at the same time.

``tests/test_cycle_close.py`` covers idempotency *sequentially* -- close, then
close again. That is a different property from the one
``CycleCloseService.close_subscription``'s docstring leans on hardest: two sweeps
of the same subscription running **at the same time** serialise on
``SELECT ... FOR UPDATE``, so the period is charged once. It is also the
highest-severity failure this service has, because the failure is a double
charge to a real customer.

Through 0.5.0 this package had no concurrency test of any kind -- a repo-wide
grep of ``tests/`` for ``threading`` returned nothing -- while shipping
behaviour that depends on a row lock holding.

**Why this needs a real database.** SQLite has no row locks. Django checks
``has_select_for_update`` and drops the clause rather than raising, so under the
suite's default SQLite settings ``close_subscription`` runs a plain ``SELECT``
and a two-thread test here would pass exactly as happily against no lock at all
-- the worst kind of green. So this module skips unless the configured database
really takes the lock; ``tests/settings.py`` switches to Postgres when
``VINTA_BILLING_TEST_POSTGRES_HOST`` is set, and ``tox -e postgres`` (which CI
runs) sets it.

The test is self-validating rather than needing a negative control. Under a
correct lock exactly one thread finds a period to close and the other re-reads
the already-rolled period and does nothing, so the per-thread verdict is
``[0, 1]``. A broken lock lets both threads read the un-rolled period and both
close it -- ``[1, 1]``, two charge attempts -- which the assertion fails on
loudly. Monkeypatching ``select_for_update`` out to build a negative control
would be fragile, and would not prove much anyway: the overage key is derived
from ``period_start``, so a provider would dedup a lock-less double *attempt*
down to one distinct key regardless. The fact that matters is "exactly one
``create_payment`` **call**", which the positive assertion pins directly.
"""

from __future__ import annotations

import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from dateutil.relativedelta import relativedelta
from django.db import connection

from vinta_billing.constants import BillingInterval, BillingState, LimitKind
from vinta_billing.models import MeteredOccurrence, Payment, SubscriptionPlanLimit
from vinta_billing.services.cycle_close_service import CycleCloseService, overage_idempotency_key


pytestmark = pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason=(
        "needs a database with row locks -- SQLite silently drops SELECT ... FOR UPDATE, "
        "so this would pass against a lock that was never taken. Run `tox -e postgres`."
    ),
)


PERIOD_START = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC)
AFTER_PERIOD = datetime.datetime(2026, 7, 2, tzinfo=datetime.UTC)

BARRIER_TIMEOUT_SECONDS = 10
RESULT_TIMEOUT_SECONDS = 30
#: The lock winner holds the subscription row for this long (it sleeps mid-charge),
#: so the other thread is reliably parked on ``select_for_update`` rather than
#: racing past it. This is what makes the two transactions provably overlap.
RACE_WINDOW_SECONDS = 0.5


class SleepingPaymentService:
    """Stands in for the provider, and widens the race window.

    ``create_payment`` sleeps before recording, so the thread that won the row
    lock is still inside its transaction when the other thread arrives -- the
    overlap the lock has to survive.

    Returns a real ``Payment`` row on every call regardless of key, exactly like
    the real ``PaymentService.create_payment``: the local row is not what
    deduplicates, the provider is, and ``BillingPeriodSummary.payment`` is a
    genuine foreign key that needs something the database can reference.
    """

    def __init__(self, billing_profile, race_window_seconds: float = 0.0):
        self._billing_profile = billing_profile
        self._race_window = race_window_seconds
        self._lock = threading.Lock()
        self.keys: list[str] = []

    def create_payment(self, **kwargs):
        if self._race_window:
            threading.Event().wait(self._race_window)
        with self._lock:
            key = kwargs.get("idempotency_key", "")
            self.keys.append(key)
            return Payment.objects.create(
                billing_profile=self._billing_profile,
                value=kwargs["amount"],
                currency=kwargs["currency"],
                payment_provider="stripe",
                external_id=f"ext-{len(self.keys)}",
                status="approved",
                original_status="approved",
                payment_method=kwargs["payment_method"],
                description=kwargs["description"],
            )


@pytest.fixture
def closable(db, organization, plan, make_subscription):
    """A billing-root subscription on a finite postpaid allowance, pinned to a
    monthly cycle that has already ended so a close has real work to do."""
    subscription = make_subscription(
        organization,
        plan,
        billing_interval=BillingInterval.MONTHLY,
        billing_state=BillingState.ACTIVE,
        current_period_start=PERIOD_START,
        current_period_end=PERIOD_END,
    )
    SubscriptionPlanLimit.objects.filter(
        subscription=subscription, resource_key="event_occurrences"
    ).update(limit_value=0, kind=LimitKind.POSTPAID, overage_unit_price=Decimal("0.50"))
    return subscription


def _seed_overage(subscription, organization, count):
    MeteredOccurrence.objects.bulk_create(
        MeteredOccurrence(
            organization=organization,
            subscription=subscription,
            event_id=index,
            occurrence_start=PERIOD_START + datetime.timedelta(days=index),
            billing_period_start=PERIOD_START,
            is_within_allowance=False,
            unit_price=Decimal("0.50"),
        )
        for index in range(1, count + 1)
    )


def _two_concurrent_closes(service, subscription):
    """Both threads call ``close_subscription`` for the same subscription at
    once. Returns each thread's count of periods closed."""
    start_barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_SECONDS)

    def close_once():
        try:
            start_barrier.wait()
            return len(service.close_subscription(subscription, now=AFTER_PERIOD))
        finally:
            # Each thread holds its own connection; leaking it holds the row lock
            # past the test and wedges whatever runs next.
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(close_once) for _ in range(2)]
        return [future.result(timeout=RESULT_TIMEOUT_SECONDS) for future in futures]


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_closes_charge_the_period_exactly_once(
    closable, organization, billing_profile
):
    """The claim ``close_subscription``'s docstring makes: a concurrent sweep
    blocks until this one commits, then re-reads the rolled period and finds
    nothing left to close."""
    _seed_overage(closable, organization, 2)  # 2 x 0.50 = 1.00
    payment_service = SleepingPaymentService(
        billing_profile, race_window_seconds=RACE_WINDOW_SECONDS
    )
    service = CycleCloseService(payment_service=payment_service)

    closed_counts = _two_concurrent_closes(service, closable)

    # One closer, one no-op. `[1, 1]` here means the lock did not hold and both
    # threads closed the same period.
    assert sorted(closed_counts) == [0, 1], f"expected one closer, got {closed_counts}"
    # The provider was asked to charge exactly once -- a single call, not two
    # calls deduplicated down by a shared key.
    assert payment_service.keys == [overage_idempotency_key(closable, PERIOD_START)]
    # And the period rolled one month, not two.
    closable.refresh_from_db()
    assert closable.current_period_start == PERIOD_END
    assert closable.current_period_end == PERIOD_END + relativedelta(months=1)


@pytest.mark.django_db(transaction=True)
def test_the_loser_actually_waits_for_the_winner_rather_than_failing_fast(
    closable, organization, billing_profile
):
    """The lock has to *block*, not skip. A `select_for_update(nowait=True)` or a
    `skip_locked` would also produce ``[0, 1]`` -- and would silently drop a
    subscription from a sweep whenever two sweeps overlapped, rather than closing
    it a moment later.

    The winner sleeps ``RACE_WINDOW_SECONDS`` inside its transaction, so if the
    loser were failing fast the whole thing would finish in well under that.
    """
    _seed_overage(closable, organization, 1)
    payment_service = SleepingPaymentService(
        billing_profile, race_window_seconds=RACE_WINDOW_SECONDS
    )
    service = CycleCloseService(payment_service=payment_service)

    started = datetime.datetime.now(datetime.UTC)
    closed_counts = _two_concurrent_closes(service, closable)
    elapsed = (datetime.datetime.now(datetime.UTC) - started).total_seconds()

    assert sorted(closed_counts) == [0, 1]
    assert elapsed >= RACE_WINDOW_SECONDS, (
        "both threads returned faster than the winner's own held transaction, so the "
        f"loser cannot have waited on the lock (elapsed {elapsed:.3f}s)"
    )
