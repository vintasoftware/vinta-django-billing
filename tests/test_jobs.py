"""The recurring work, and where it gets its services from.

``vinta_billing.jobs`` is the one place in this package that runs without a
request. Everything else that builds a service -- the shipped viewsets, the
admin -- asks ``resolve_service``, so ``VINTA_BILLING['SERVICE_CONTAINER']``
decides which container answers. Until 0.6.0 the jobs imported this package's
own factories directly and a project's container was never consulted, which is
what these tests are here to keep from coming back.

The stakes are not abstract. A project pointing ``SERVICE_CONTAINER`` at its own
container so a test could drive a deliberately-crippled provider adapter (empty
API key, so a stray real call fails loudly) found the sweeps building a *second*
service graph from this package's factories instead -- one wired to the real
``STRIPE_SECRET_KEY`` from its environment. The test believed it was driving a
fake and was one code path away from driving a live credential.
"""

from __future__ import annotations

import datetime

import pytest
from django.test import override_settings
from django.utils import timezone

from vinta_billing import jobs
from vinta_billing.constants import BillingState


pytestmark = pytest.mark.django_db


class RecordingService:
    """One double standing in for all four services.

    Each sweep calls exactly one method on the service it resolves, and each of
    those methods takes the subscription as its first argument, so a single
    recorder covers the four of them without pretending to be four classes.
    """

    def __init__(self, container):
        self.container = container

    def _record(self, name, subscription):
        self.container.calls.append((name, subscription.pk))

    def meter_occurrences_for_period(self, subscription, window_start, window_end):
        self._record("meter", subscription)
        return _MeteringResult(subscription.pk, window_start, window_end)

    def process_subscription(self, subscription):
        self._record("dun", subscription)

    def close_subscription(self, subscription):
        self._record("close", subscription)
        return []

    def check_subscription(self, subscription):
        self._record("warn", subscription)


class _MeteringResult:
    """Only the attributes ``meter_subscription_event_occurrences`` logs."""

    def __init__(self, subscription_id, window_start, window_end):
        self.subscription_id = subscription_id
        self.window_start = window_start
        self.window_end = window_end
        self.occurrences_seen = 0
        self.occurrences_recorded = 0


class ProjectContainer:
    """A project's own container, in the shape ``dependency_injector`` builds:
    a provider is an attribute named after the service it builds.

    ``resolve_service`` accepts this spelling and this package's own
    ``get_<name>()`` spelling both -- see
    ``tests/test_integration_seams.py::TestTheServiceContainerSeam``. This one
    deliberately offers *only* the bare spelling, so a job that reached past
    ``resolve_service`` into this package's container module could not be
    mistaken for one that went through it.
    """

    def __init__(self):
        self.calls: list[tuple[str, int]] = []
        self.built: list[str] = []

    def _build(self, name):
        self.built.append(name)
        return RecordingService(self)

    def metering_service(self):
        return self._build("metering_service")

    def dunning_service(self):
        return self._build("dunning_service")

    def cycle_close_service(self):
        return self._build("cycle_close_service")

    def usage_warning_service(self):
        return self._build("usage_warning_service")


@pytest.fixture
def container():
    return ProjectContainer()


@pytest.fixture
def due_subscription(subscription):
    """The suite's active subscription, with its period already ended so the
    cycle-close sweep has work to do."""
    subscription.current_period_start = timezone.now() - datetime.timedelta(days=40)
    subscription.current_period_end = timezone.now() - datetime.timedelta(days=10)
    subscription.save(update_fields=["current_period_start", "current_period_end"])
    return subscription


class TestTheSweepsHonourTheServiceContainer:
    """Each of the four sweeps has to build its service through
    ``resolve_service``, so a project's ``SERVICE_CONTAINER`` governs a beat
    tick exactly as it governs a request."""

    def test_the_metering_sweep_uses_the_configured_container(self, container, due_subscription):
        with override_settings(VINTA_BILLING={"SERVICE_CONTAINER": container}):
            jobs.meter_event_occurrences()

        assert container.built == ["metering_service"]
        assert container.calls == [("meter", due_subscription.pk)]

    def test_the_dunning_sweep_uses_the_configured_container(self, container, subscription):
        subscription.billing_state = BillingState.GRACE
        subscription.save(update_fields=["billing_state"])

        with override_settings(VINTA_BILLING={"SERVICE_CONTAINER": container}):
            jobs.process_dunning()

        assert container.built == ["dunning_service"]
        assert container.calls == [("dun", subscription.pk)]

    def test_the_cycle_close_sweep_uses_the_configured_container(self, container, due_subscription):
        with override_settings(VINTA_BILLING={"SERVICE_CONTAINER": container}):
            jobs.close_billing_periods()

        assert container.built == ["cycle_close_service"]
        assert container.calls == [("close", due_subscription.pk)]

    def test_the_usage_warning_sweep_uses_the_configured_container(self, container, subscription):
        with override_settings(VINTA_BILLING={"SERVICE_CONTAINER": container}):
            jobs.check_approaching_limits()

        assert container.built == ["usage_warning_service"]
        assert container.calls == [("warn", subscription.pk)]

    def test_an_explicitly_passed_service_still_wins_over_the_container(
        self, container, subscription
    ):
        """Unchanged from before the seam existed: a caller that hands a job its
        service is never asked to configure a container, and the container is
        not consulted at all."""
        mine = RecordingService(container)

        with override_settings(VINTA_BILLING={"SERVICE_CONTAINER": container}):
            jobs.process_dunning_for_subscription(subscription.pk, dunning_service=mine)

        assert container.built == []
        assert container.calls == [("dun", subscription.pk)]


class TestTheDispatcherSeamStillWorksAlongsideIt:
    """The two seams compose: a project can queue the per-subscription jobs
    *and* have its own container build the services they resolve. Resolving each
    service by hand and passing it in -- the workaround ``SERVICE_CONTAINER``
    being ignored forced -- gives up the first of these, because a sweep hands
    its dispatcher nothing but a job and a subscription id."""

    def test_a_configured_dispatcher_receives_the_job_and_its_arguments(
        self, container, subscription
    ):
        subscription.billing_state = BillingState.GRACE
        subscription.save(update_fields=["billing_state"])
        dispatched: list[tuple] = []

        with override_settings(
            VINTA_BILLING={
                "SERVICE_CONTAINER": container,
                "JOB_DISPATCHER": lambda job, *args: dispatched.append((job, args)),
            }
        ):
            jobs.process_dunning()

        assert dispatched == [(jobs.process_dunning_for_subscription, (subscription.pk,))]
        # Nothing ran, so nothing was resolved -- the queued job resolves its own
        # service through the same container when the worker picks it up.
        assert container.built == []

        with override_settings(VINTA_BILLING={"SERVICE_CONTAINER": container}):
            job, args = dispatched[0]
            job(*args)

        assert container.built == ["dunning_service"]
        assert container.calls == [("dun", subscription.pk)]
