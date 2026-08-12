"""Resolves the browser-safe half of a payment provider's credentials.

This module reads the ``PUBLISHABLE_KEY`` / ``PUBLIC_KEY`` entries out of
``VINTA_BILLING['PROVIDERS']`` **directly** and must never import, construct, or
otherwise touch a payment adapter (``billing.services.payment_adapters`` /
``billing.services.subscription_adapters``) or read any *secret* key out of that same
entry (``API_KEY``, ``ACCESS_TOKEN``, ``WEBHOOK_SECRET``). Those adapters hold the secret
keys used to authenticate outbound calls to the provider; this module backs the
unauthenticated and authenticated-but-read-only provider-credentials endpoints
(``billing.views.PaymentProviderViewSet``), so it must have no code path that could ever
serialize a secret onto a response. Keep it that way -- do not import an adapter here, even
transitively, and do not add a helper that reads a secret key into this module.
"""

from dataclasses import dataclass

from billing.conf import get_provider_config
from billing.constants import PaymentProviders
from billing.exceptions import PaymentProviderNotConfiguredError


@dataclass(frozen=True)
class PublicProviderCredentials:
    """The non-secret, browser-safe half of a provider's credentials.

    Deliberately separate from the adapter's constructor arguments: the adapter holds the
    *secret* key (the provider entry's ``API_KEY`` / ``ACCESS_TOKEN``) and must never be a
    source these values are read through, so that no refactor can accidentally serialize a
    secret onto a response.
    """

    provider: str
    stripe_publishable_key: str | None = None
    mercadopago_public_key: str | None = None


def resolve_public_credentials(provider: str) -> PublicProviderCredentials:
    """The public credentials for ``provider``, with only the matching provider's field
    populated.

    :raises PaymentProviderNotConfiguredError: ``provider`` is not a real, registered
        provider (an unknown slug), or it is a real provider whose public key setting is
        empty in this deployment. Both cases collapse to the same error here -- unlike the
        webhook views' ``UnknownPaymentProviderError``/``PaymentProviderNotConfiguredError``
        split, the credentials endpoints have nothing useful to say beyond "this provider
        cannot be used to render a payment form right now" (``GET
        /billing/payment-provider/`` and its ``/default/`` sibling).
    """
    if provider == PaymentProviders.STRIPE:
        publishable_key = get_provider_config(provider).get("PUBLISHABLE_KEY") or ""
        if not publishable_key:
            raise PaymentProviderNotConfiguredError(provider)
        return PublicProviderCredentials(provider=provider, stripe_publishable_key=publishable_key)

    if provider == PaymentProviders.MERCADOPAGO:
        public_key = get_provider_config(provider).get("PUBLIC_KEY") or ""
        if not public_key:
            raise PaymentProviderNotConfiguredError(provider)
        return PublicProviderCredentials(provider=provider, mercadopago_public_key=public_key)

    raise PaymentProviderNotConfiguredError(provider)
