"""The payment adapters this package ships, and how a project adds its own.

Provider SDKs are optional extras, so an adapter whose SDK is not installed is
skipped rather than raising on import: a project charging only through Stripe
should not be made to install MercadoPago's client to start up.
"""

from __future__ import annotations

import logging
from typing import Any

from billing.provider_slugs import MERCADOPAGO, STRIPE


logger = logging.getLogger(__name__)

#: Providers registered beyond the built-in two, by slug.
_extra_adapters: dict[str, type[Any]] = {}

_BUILTIN = {
    STRIPE: (
        "billing.services.payment_adapters.stripe_payment_adapter",
        "StripePaymentAdapter",
    ),
    MERCADOPAGO: (
        "billing.services.payment_adapters.mercadopago_payment_adapter",
        "MercadoPagoPaymentAdapter",
    ),
}


def register_payment_adapter(slug: str, adapter_class: type[Any]) -> None:
    """Make ``adapter_class`` resolvable under ``slug``."""
    _extra_adapters[slug] = adapter_class


def get_payment_provider_registry() -> dict[str, type[Any]]:
    """Every payment adapter this deployment can drive, by slug."""
    from django.utils.module_loading import import_string

    registry: dict[str, type[Any]] = {}
    for slug, (module, name) in _BUILTIN.items():
        try:
            registry[slug] = import_string("%s.%s" % (module, name))
        except ImportError:
            # The provider's SDK is not installed. Not an error: the extras
            # exist so a project installs only the providers it charges through.
            logger.debug("Payment adapter for %s is unavailable; skipping.", slug)
    registry.update(_extra_adapters)
    return registry
