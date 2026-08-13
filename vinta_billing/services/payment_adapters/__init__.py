"""The payment adapters this package ships, and how a project adds its own.

Provider SDKs are optional extras, so an adapter whose SDK is not installed is
skipped rather than raising on import: a project charging only through Stripe
should not be made to install MercadoPago's client to start up.

The registry hands back *built* adapters, not classes. Each one is constructed
from its ``VINTA_BILLING['PROVIDERS']`` entry through
:meth:`BasePaymentAdapter.from_config`, so a caller that resolved an adapter can
immediately drive it, and ``is_configured`` answers off the credential this
deployment actually holds rather than off a class attribute that is always
truthy.
"""

from __future__ import annotations

import logging
from typing import Any

from vinta_billing.provider_slugs import MERCADOPAGO, STRIPE


logger = logging.getLogger(__name__)

#: Providers registered beyond the built-in two, by slug.
_extra_adapters: dict[str, type[Any]] = {}

_BUILTIN = {
    STRIPE: (
        "vinta_billing.services.payment_adapters.stripe_payment_adapter",
        "StripePaymentAdapter",
    ),
    MERCADOPAGO: (
        "vinta_billing.services.payment_adapters.mercadopago_payment_adapter",
        "MercadoPagoPaymentAdapter",
    ),
}


def register_payment_adapter(slug: str, adapter_class: type[Any]) -> None:
    """Make ``adapter_class`` resolvable under ``slug``."""
    _extra_adapters[slug] = adapter_class


def get_payment_adapter_classes() -> dict[str, type[Any]]:
    """Every payment adapter class registered in this process, by slug.

    Separate from :func:`get_payment_provider_registry` because a caller that
    only wants to know *whether* a slug is a provider this deployment knows
    (registration, validation, a conformance test) should not have to build an
    adapter to find out.
    """
    from django.utils.module_loading import import_string

    classes: dict[str, type[Any]] = {}
    for slug, (module, name) in _BUILTIN.items():
        try:
            classes[slug] = import_string("%s.%s" % (module, name))
        except ImportError:
            # The provider's SDK is not installed. Not an error: the extras
            # exist so a project installs only the providers it charges through.
            logger.debug("Payment adapter for %s is unavailable; skipping.", slug)
    classes.update(_extra_adapters)
    return classes


def get_payment_provider_registry() -> dict[str, Any]:
    """Every payment adapter this deployment can drive, built and by slug.

    Built from ``VINTA_BILLING['PROVIDERS']``; a provider with no entry is still
    included, holding an empty credential, so its inbound webhook route resolves
    while every outbound call site refuses it through ``is_configured``.
    """
    from vinta_billing.conf import get_provider_config

    return {
        slug: adapter_class.from_config(get_provider_config(slug))
        for slug, adapter_class in get_payment_adapter_classes().items()
    }
