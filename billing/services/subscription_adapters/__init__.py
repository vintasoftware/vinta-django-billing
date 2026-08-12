"""The subscription adapters this package ships, and how a project adds its own.

Same optional-SDK rule as :mod:`billing.services.payment_adapters`: an adapter
whose provider SDK is missing is skipped, not raised on.
"""

from __future__ import annotations

import logging
from typing import Any

from billing.provider_slugs import MERCADOPAGO, STRIPE


logger = logging.getLogger(__name__)

_extra_adapters: dict[str, type[Any]] = {}

_BUILTIN = {
    STRIPE: (
        "billing.services.subscription_adapters.stripe_subscription_adapter",
        "StripeSubscriptionAdapter",
    ),
    MERCADOPAGO: (
        "billing.services.subscription_adapters.mercadopago_subscription_adapter",
        "MercadoPagoSubscriptionAdapter",
    ),
}


def register_subscription_adapter(slug: str, adapter_class: type[Any]) -> None:
    """Make ``adapter_class`` resolvable under ``slug``."""
    _extra_adapters[slug] = adapter_class


def get_subscription_provider_registry() -> dict[str, type[Any]]:
    """Every subscription adapter this deployment can drive, by slug."""
    from django.utils.module_loading import import_string

    registry: dict[str, type[Any]] = {}
    for slug, (module, name) in _BUILTIN.items():
        try:
            registry[slug] = import_string("%s.%s" % (module, name))
        except ImportError:
            logger.debug("Subscription adapter for %s is unavailable; skipping.", slug)
    registry.update(_extra_adapters)
    return registry
