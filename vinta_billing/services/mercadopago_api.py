"""What every MercadoPago adapter does with a request before and after the SDK.

The SDK never raises on an error answer: it returns ``{"status": <http status>,
"response": <body>}`` whatever happened, so each call site has to look at the
status itself. Reading ``response["id"]`` off a refused request is what turned
every MercadoPago validation error into a ``KeyError`` before 0.9.1.
"""

import logging
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from vinta_billing.constants import PaymentProviders
from vinta_billing.exceptions import ProviderRequestRejectedError


logger = logging.getLogger(__name__)

CENTS = Decimal("0.01")

#: Hosts MercadoPago refuses as a ``notification_url`` (code 4020), and which a
#: webhook could not reach anyway.
LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1"})


def numeric_amount(value: Decimal) -> float:
    """``value`` as the JSON number the Payments and Subscriptions APIs require.

    They answer a string amount with ``400 "transaction_amount attribute must be
    numeric"``. Rounded to cents first, so the float is the nearest one to an
    exact two-decimal value and serializes as one (``150.5``, ``100.25``).
    """
    return float(Decimal(value).quantize(CENTS))


def is_local_host(host: str) -> bool:
    """Whether ``host`` (optionally with a port) is a loopback name."""
    hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    hostname = hostname.lower()
    return hostname in LOCAL_HOSTS or hostname.endswith(".localhost")


def _causes(body: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """``(code, description)`` pairs from either error shape MercadoPago uses:
    ``cause[]`` with ``code``/``description`` (Payments, Subscriptions) and
    ``errors[]`` with ``code``/``message`` (Orders)."""
    pairs: list[tuple[str, str]] = []
    for item in body.get("cause") or ():
        if isinstance(item, Mapping):
            pairs.append((str(item.get("code", "")), str(item.get("description", ""))))
    for item in body.get("errors") or ():
        if isinstance(item, Mapping):
            pairs.append((str(item.get("code", "")), str(item.get("message", ""))))
    return tuple(pairs)


def rejection_error(result: Mapping[str, Any]) -> ProviderRequestRejectedError:
    """The ``ProviderRequestRejectedError`` describing an error ``result``."""
    body = result.get("response")
    body = body if isinstance(body, Mapping) else {}
    return ProviderRequestRejectedError(
        PaymentProviders.MERCADOPAGO,
        result.get("status"),
        str(body.get("message") or body.get("error") or ""),
        _causes(body),
    )


def is_success(result: Mapping[str, Any]) -> bool:
    """Whether the SDK ``result`` carries a 2xx status.

    A result with no ``status`` at all is taken as a success: the SDK always sets
    one, so its absence only means a caller built the dict by hand.
    """
    http_status = result.get("status")
    return http_status is None or 200 <= int(http_status) < 300


def response_or_raise(result: Mapping[str, Any], *, require_id: bool = True) -> Any:
    """The body of a successful SDK ``result``; raises
    ``ProviderRequestRejectedError`` for anything else.

    ``require_id`` also refuses a 2xx body without an ``id`` -- every create and
    get answers with one, so its absence means the body is not the resource.
    Logs only the status and MercadoPago's own error codes, never the body, which
    can echo payer data back.
    """
    body = result.get("response")
    if is_success(result) and (
        not require_id or (isinstance(body, Mapping) and body.get("id") is not None)
    ):
        return body
    error = rejection_error(result)
    logger.warning(
        "MercadoPago refused a request: http_status=%s causes=%s",
        error.http_status,
        [code for code, _ in error.causes],
    )
    raise error
