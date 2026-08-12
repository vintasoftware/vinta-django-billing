"""The occurrence ledger: idempotency, allowance consumption and overage pricing.

This is the path that turns activity into money, and its two hardest properties
are both about repetition -- a window swept twice must not bill twice, and an
overlapping window must not push genuinely new occurrences into overage by
letting already-recorded ones consume the allowance again.
"""

import datetime
from decimal import Decimal
from typing import ClassVar

import pytest
from django.test import override_settings

from billing.constants import BillingInterval
from billing.metering import Occurrence
from billing.models import MeteredOccurrence
from billing.services.container import get_metering_service


pytestmark = pytest.mark.django_db


def utc(year, month, day, hour=0):
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.UTC)


WINDOW_START = utc(2026, 3, 1)
WINDOW_END = utc(2026, 4, 1)


class ListSource:
    """An occurrence source returning a fixed list, ignoring the window.

    The window filter lives in `expand_occurrence_identities`, not in the source,
    so returning everything is how these tests exercise that filter.
    """

    occurrences: ClassVar[list] = []

    def iter_occurrences(self, organization_ids, window_start, window_end):
        return list(self.occurrences)

    def describe(self, external_ids):
        return {}


@pytest.fixture
def source():
    """Installs a per-test occurrence list as the configured source."""

    class Source(ListSource):
        occurrences: ClassVar[list] = []

    with override_settings(
        VINTA_BILLING={"METERED_RESOURCE_KEY": "event_occurrences", "OCCURRENCE_SOURCE": Source()}
    ):
        yield Source


@pytest.fixture
def metered_subscription(organization, plan, make_subscription):
    """A subscription with an allowance of 2 metered occurrences at $0.10 over."""
    plan.limits.filter(resource_key="event_occurrences").update(
        limit_value=2, overage_unit_price=Decimal("0.10")
    )
    return make_subscription(
        organization,
        plan,
        billing_interval=BillingInterval.MONTHLY,
        current_period_start=WINDOW_START,
        current_period_end=WINDOW_END,
    )


def occurrence(organization, day, external_id):
    return Occurrence(
        external_id=external_id,
        organization_id=organization.pk,
        occurred_at=utc(2026, 3, day),
    )


class TestWindowHandling:
    def test_an_inverted_window_is_refused_without_writing(
        self, source, organization, metered_subscription
    ):
        source.occurrences = [occurrence(organization, 5, 1)]

        result = get_metering_service().meter_occurrences_for_period(
            metered_subscription, WINDOW_END, WINDOW_START
        )

        assert result.occurrences_recorded == 0
        assert not MeteredOccurrence.objects.exists()

    def test_an_occurrence_before_the_window_is_ignored(
        self, source, organization, metered_subscription
    ):
        source.occurrences = [
            Occurrence(external_id=1, organization_id=organization.pk, occurred_at=utc(2026, 2, 20))
        ]

        result = get_metering_service().meter_occurrences_for_period(
            metered_subscription, WINDOW_START, WINDOW_END
        )

        assert result.occurrences_seen == 0

    def test_the_window_end_is_exclusive(self, source, organization, metered_subscription):
        """An occurrence exactly at the boundary belongs to the next window --
        billed once there, not twice and not never."""
        source.occurrences = [
            Occurrence(external_id=1, organization_id=organization.pk, occurred_at=WINDOW_END)
        ]

        result = get_metering_service().meter_occurrences_for_period(
            metered_subscription, WINDOW_START, WINDOW_END
        )

        assert result.occurrences_seen == 0

    def test_an_occurrence_outside_the_pool_is_dropped(
        self, source, other_organization, metered_subscription
    ):
        """Billing another tenant for it would be far worse than under-counting."""
        source.occurrences = [occurrence(other_organization, 5, 1)]

        result = get_metering_service().meter_occurrences_for_period(
            metered_subscription, WINDOW_START, WINDOW_END
        )

        assert result.occurrences_seen == 0


class TestIdempotency:
    def test_records_each_occurrence_once(self, source, organization, metered_subscription):
        source.occurrences = [occurrence(organization, 5, 1), occurrence(organization, 6, 2)]

        result = get_metering_service().meter_occurrences_for_period(
            metered_subscription, WINDOW_START, WINDOW_END
        )

        assert result.occurrences_recorded == 2
        assert MeteredOccurrence.objects.count() == 2

    def test_a_repeated_sweep_records_nothing_new(self, source, organization, metered_subscription):
        """This is how a missed run heals: re-sweeping is always safe."""
        source.occurrences = [occurrence(organization, 5, 1), occurrence(organization, 6, 2)]
        service = get_metering_service()
        service.meter_occurrences_for_period(metered_subscription, WINDOW_START, WINDOW_END)

        second = service.meter_occurrences_for_period(
            metered_subscription, WINDOW_START, WINDOW_END
        )

        assert second.occurrences_recorded == 0
        assert MeteredOccurrence.objects.count() == 2

    def test_a_duplicate_from_the_source_is_collapsed(
        self, source, organization, metered_subscription
    ):
        """Deduplicated on the identity tuple before insertion, so
        `occurrences_seen` counts distinct occurrences rather than source output.
        """
        source.occurrences = [occurrence(organization, 5, 1), occurrence(organization, 5, 1)]

        result = get_metering_service().meter_occurrences_for_period(
            metered_subscription, WINDOW_START, WINDOW_END
        )

        assert result.occurrences_seen == 1
        assert MeteredOccurrence.objects.count() == 1

    def test_the_same_event_at_two_times_is_two_occurrences(
        self, source, organization, metered_subscription
    ):
        """A recurring series shares one external id across its occurrences."""
        source.occurrences = [occurrence(organization, 5, 1), occurrence(organization, 6, 1)]

        result = get_metering_service().meter_occurrences_for_period(
            metered_subscription, WINDOW_START, WINDOW_END
        )

        assert result.occurrences_recorded == 2


class TestAllowanceAndPricing:
    def test_occurrences_inside_the_allowance_are_free(
        self, source, organization, metered_subscription
    ):
        source.occurrences = [occurrence(organization, 5, 1), occurrence(organization, 6, 2)]

        get_metering_service().meter_occurrences_for_period(
            metered_subscription, WINDOW_START, WINDOW_END
        )

        assert MeteredOccurrence.objects.filter(is_within_allowance=True).count() == 2
        assert set(MeteredOccurrence.objects.values_list("unit_price", flat=True)) == {Decimal("0")}

    def test_occurrences_past_the_allowance_are_priced(
        self, source, organization, metered_subscription
    ):
        source.occurrences = [occurrence(organization, day, day) for day in (5, 6, 7, 8)]

        get_metering_service().meter_occurrences_for_period(
            metered_subscription, WINDOW_START, WINDOW_END
        )

        overage = MeteredOccurrence.objects.filter(is_within_allowance=False)
        assert overage.count() == 2
        assert all(row.unit_price == Decimal("0.10") for row in overage)

    def test_the_allowance_is_consumed_chronologically(
        self, source, organization, metered_subscription
    ):
        """The earliest two occurrences are the free ones, whatever order the
        source reported them in."""
        source.occurrences = [occurrence(organization, day, day) for day in (8, 5, 7, 6)]

        get_metering_service().meter_occurrences_for_period(
            metered_subscription, WINDOW_START, WINDOW_END
        )

        free_days = sorted(
            row.occurrence_start.day
            for row in MeteredOccurrence.objects.filter(is_within_allowance=True)
        )
        assert free_days == [5, 6]

    def test_an_overlapping_sweep_does_not_re_consume_the_allowance(
        self, source, organization, metered_subscription
    ):
        """The bug this guards: without filtering already-recorded identities
        before ranking, an occurrence from an earlier sweep would consume an
        allowance slot again and push a genuinely new one into overage.
        """
        service = get_metering_service()
        source.occurrences = [occurrence(organization, 5, 5)]
        service.meter_occurrences_for_period(metered_subscription, WINDOW_START, WINDOW_END)

        source.occurrences = [occurrence(organization, 5, 5), occurrence(organization, 6, 6)]
        service.meter_occurrences_for_period(metered_subscription, WINDOW_START, WINDOW_END)

        assert MeteredOccurrence.objects.filter(is_within_allowance=True).count() == 2
        assert MeteredOccurrence.objects.filter(is_within_allowance=False).count() == 0

    def test_an_unlimited_allowance_never_charges_overage(
        self, source, organization, plan, make_subscription
    ):
        plan.limits.filter(resource_key="event_occurrences").update(limit_value=None)
        subscription = make_subscription(
            organization,
            plan,
            current_period_start=WINDOW_START,
            current_period_end=WINDOW_END,
        )
        source.occurrences = [occurrence(organization, day, day) for day in (5, 6, 7, 8)]

        get_metering_service().meter_occurrences_for_period(subscription, WINDOW_START, WINDOW_END)

        assert MeteredOccurrence.objects.filter(is_within_allowance=False).count() == 0

    def test_a_zero_allowance_prices_everything(
        self, source, organization, plan, make_subscription
    ):
        plan.limits.filter(resource_key="event_occurrences").update(
            limit_value=0, overage_unit_price=Decimal("0.25")
        )
        subscription = make_subscription(
            organization,
            plan,
            current_period_start=WINDOW_START,
            current_period_end=WINDOW_END,
        )
        source.occurrences = [occurrence(organization, 5, 1)]

        get_metering_service().meter_occurrences_for_period(subscription, WINDOW_START, WINDOW_END)

        row = MeteredOccurrence.objects.get()
        assert row.is_within_allowance is False
        assert row.unit_price == Decimal("0.25")


class TestPeriodStamping:
    def test_rows_are_stamped_with_the_period_they_happened_in(
        self, source, organization, metered_subscription
    ):
        source.occurrences = [occurrence(organization, 5, 1)]

        get_metering_service().meter_occurrences_for_period(
            metered_subscription, WINDOW_START, WINDOW_END
        )

        assert MeteredOccurrence.objects.get().billing_period_start == WINDOW_START

    def test_a_window_spanning_a_boundary_starts_the_next_allowance_fresh(
        self, source, organization, metered_subscription
    ):
        """Allowance is per period, so a two-month sweep grants two allowances."""
        source.occurrences = [
            occurrence(organization, 5, 1),
            occurrence(organization, 6, 2),
            occurrence(organization, 7, 3),
            Occurrence(external_id=4, organization_id=organization.pk, occurred_at=utc(2026, 4, 5)),
        ]

        get_metering_service().meter_occurrences_for_period(
            metered_subscription, WINDOW_START, utc(2026, 5, 1)
        )

        april = MeteredOccurrence.objects.filter(billing_period_start=WINDOW_END)
        assert april.count() == 1
        assert april.get().is_within_allowance is True
