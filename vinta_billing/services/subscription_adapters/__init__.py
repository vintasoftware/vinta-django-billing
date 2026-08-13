"""The subscription adapters this package ships, and how a project adds its own.

Same optional-SDK rule as :mod:`vinta_billing.services.payment_adapters`: an adapter
whose provider SDK is missing is skipped, not raised on. Same build rule too --
the registry hands back adapters constructed from
``VINTA_BILLING['PROVIDERS']``, not classes.
"""

from __future__ import annotations

import logging
from typing import Any

from vinta_billing.provider_slugs import MERCADOPAGO, STRIPE


logger = logging.getLogger(__name__)

_extra_adapters: dict[str, type[Any]] = {}

_BUILTIN = {
    STRIPE: (
        "vinta_billing.services.subscription_adapters.stripe_subscription_adapter",
        "StripeSubscriptionAdapter",
    ),
    MERCADOPAGO: (
        "vinta_billing.services.subscription_adapters.mercadopago_subscription_adapter",
        "MercadoPagoSubscriptionAdapter",
    ),
}


def register_subscription_adapter(slug: str, adapter_class: type[Any]) -> None:
    """Make ``adapter_class`` resolvable under ``slug``."""
    _extra_adapters[slug] = adapter_class


def get_subscription_adapter_classes() -> dict[str, type[Any]]:
    """Every subscription adapter class registered in this process, by slug.

    See :func:`vinta_billing.services.payment_adapters.get_payment_adapter_classes`
    for why this is separate from the built registry.
    """
    from django.utils.module_loading import import_string

    classes: dict[str, type[Any]] = {}
    for slug, (module, name) in _BUILTIN.items():
        try:
            classes[slug] = import_string("%s.%s" % (module, name))
        except ImportError:
            logger.debug("Subscription adapter for %s is unavailable; skipping.", slug)
    classes.update(_extra_adapters)
    return classes


def get_subscription_provider_registry() -> dict[str, Any]:
    """Every subscription adapter this deployment can drive, built and by slug.

    See :func:`vinta_billing.services.payment_adapters.get_payment_provider_registry`.
    """
    from vinta_billing.conf import get_provider_config

    return {
        slug: adapter_class.from_config(get_provider_config(slug))
        for slug, adapter_class in get_subscription_adapter_classes().items()
    }
