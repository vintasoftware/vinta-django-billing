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

Everything above is the *default*. The shipped views and the admin do not name
this module: they call :func:`resolve_service`, which asks whatever
``VINTA_BILLING['SERVICE_CONTAINER']`` points at, and which points here until a
project says otherwise. That is what lets a project running its own container
mount the shipped routes as they are and still have its own factories build the
services -- including anything a test overrode on them, which a second,
parallel set of instances cached here could never reflect.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from importlib import import_module
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from vinta_billing.conf import get_setting
from vinta_billing.services.cycle_close_service import CycleCloseService
from vinta_billing.services.dunning_service import DunningService
from vinta_billing.services.entitlement_service import EntitlementService
from vinta_billing.services.metering_service import MeteringService
from vinta_billing.services.payment_provider_resolver import PaymentProviderResolver
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
def get_payment_provider_resolver() -> PaymentProviderResolver:
    return PaymentProviderResolver()


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
        get_payment_provider_resolver,
        get_subscription_service,
        get_metering_service,
        get_dunning_service,
        get_usage_warning_service,
        get_cycle_close_service,
    ):
        factory.cache_clear()


def _import_container(path: str) -> Any:
    """The object ``path`` names: a module, or something inside one.

    ``SERVICE_CONTAINER`` is spelled both ways in practice -- this package's own
    default names a module (``vinta_billing.services.container``), and a project
    running ``dependency_injector`` names an instance inside one
    (``myproject.di.container``). A module is tried first, and only a
    ``ModuleNotFoundError`` *about this very path* falls through to the
    attribute lookup; a module that exists but whose own imports fail raises
    from here rather than being reported as a missing attribute.
    """
    try:
        return import_module(path)
    except ModuleNotFoundError as error:
        if error.name != path:
            raise
        return import_string(path)


def get_service_container() -> Any:
    """The object the shipped views and the admin build their services from.

    ``VINTA_BILLING['SERVICE_CONTAINER']``, or this module when it is unset.
    Resolved per call rather than cached: a project's container is built in its
    app's ``ready()``, and a test that overrides a provider on it must be what
    the next request sees.
    """
    container = get_setting("SERVICE_CONTAINER")
    if container is None:
        return sys.modules[__name__]
    if not isinstance(container, str):
        # Tests and programmatic configuration hand over the container itself.
        return container
    return _import_container(container)


def resolve_service(name: str) -> Any:
    """Build the service called ``name`` through the configured container.

    ``name`` is the service's own name -- ``"payment_service"``,
    ``"entitlement_service"`` -- and two spellings of a factory for it are
    accepted, in this order:

    * ``get_<name>()``, which is how this module (and any plain module of
      factory functions) offers one;
    * ``<name>()``, which is how a ``dependency_injector`` container offers one:
      its providers are callables named after what they build.

    Both are called with no arguments, so a container has to be able to build
    each service without help from here -- which every provider already can,
    since nothing in a view knows what a service's collaborators are.
    """
    container = get_service_container()
    factory = getattr(container, f"get_{name}", None) or getattr(container, name, None)
    if factory is None:
        raise ImproperlyConfigured(
            "VINTA_BILLING['SERVICE_CONTAINER'] (%r) offers neither get_%s() nor %s(); "
            "the shipped views cannot build a %s." % (container, name, name, name)
        )
    return factory()
