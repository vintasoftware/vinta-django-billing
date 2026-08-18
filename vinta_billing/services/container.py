"""How the services find each other.

The application this came from wired these through ``dependency_injector``, with
every service method carrying ``@inject`` and ``Provide[...]`` defaults. A
library should not force a DI framework on its host, so the wiring is reduced to
what it was actually used for: constructing a service with its collaborators
already attached, and letting a test pass a double instead.

Each service still takes its collaborators as optional constructor arguments, so
a project that *does* run a container can build them itself and ignore this
module entirely.

The instances are cached per process. Every service here is stateless -- they
hold no request, no organization and no transaction -- so sharing one is safe
and saves rebuilding the object graph on every limit check.
"""

from __future__ import annotations

from functools import lru_cache

from vinta_billing.services.cycle_close_service import CycleCloseService
from vinta_billing.services.dunning_service import DunningService
from vinta_billing.services.entitlement_service import EntitlementService
from vinta_billing.services.metering_service import MeteringService
from vinta_billing.services.payment_service import PaymentService
from vinta_billing.services.subscription_service import SubscriptionService
from vinta_billing.services.usage_warning_service import UsageWarningService


@lru_cache(maxsize=1)
def get_entitlement_service() -> EntitlementService:
    return EntitlementService()


@lru_cache(maxsize=1)
def get_payment_service() -> PaymentService:
    return PaymentService()


@lru_cache(maxsize=1)
def get_subscription_service() -> SubscriptionService:
    return SubscriptionService()


@lru_cache(maxsize=1)
def get_metering_service() -> MeteringService:
    return MeteringService()


@lru_cache(maxsize=1)
def get_dunning_service() -> DunningService:
    return DunningService()


@lru_cache(maxsize=1)
def get_usage_warning_service() -> UsageWarningService:
    return UsageWarningService()


@lru_cache(maxsize=1)
def get_cycle_close_service() -> CycleCloseService:
    return CycleCloseService()


def reset_services() -> None:
    """Drop every cached service.

    For tests that reconfigure ``VINTA_BILLING`` and need the next call to build
    a service against the new settings.
    """
    for factory in (
        get_entitlement_service,
        get_payment_service,
        get_subscription_service,
        get_metering_service,
        get_dunning_service,
        get_usage_warning_service,
        get_cycle_close_service,
    ):
        factory.cache_clear()
