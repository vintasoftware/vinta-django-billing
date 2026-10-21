"""``verify_mercadopago_signature``: which ``data.id`` is signed, and how.

Shaped after a real Orders API notification from MercadoPago's "Simular
notificação" (test environment, topic ``order``), with a test secret: the
``ORD...`` id was signed exactly as sent, not lowercased as the docs describe.
"""

import hashlib
import hmac
import json
import time

import pytest

from vinta_billing.services.mercadopago_signature import verify_mercadopago_signature
from vinta_billing.services.payment_adapters import base as payment_adapter_base
from vinta_billing.services.payment_adapters.mercadopago_orders_payment_adapter import (
    MercadoPagoOrdersPaymentAdapter,
)


SECRET = "test-webhook-secret"
ORDER_ID = "ORDTST01M5EPFPG2SRJTVMQ3JEK47XZN"
REQUEST_ID = "e643f528-a721-44f5-9fc3-9ce98961920d"


def sign(signed_id: str, ts: str | None = None, request_id: str = REQUEST_ID) -> dict[str, str]:
    ts = ts or str(int(time.time()))
    manifest = f"id:{signed_id};request-id:{request_id};ts:{ts};"
    v1 = hmac.new(SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return {"X-Signature": f"ts={ts},v1={v1}", "X-Request-Id": request_id}


def order_body(data_id: str | None = ORDER_ID) -> bytes:
    body: dict = {"action": "order.processed", "api_version": "v1", "type": "order"}
    if data_id is not None:
        body["data"] = {"id": data_id}
    return json.dumps(body).encode()


def verify(body: bytes, headers: dict[str, str], query: dict[str, str] | None = None):
    return verify_mercadopago_signature(body, headers, SECRET, query)


class TestWhichFormOfTheIdIsSigned:
    def test_an_uppercase_order_id_signed_as_sent(self):
        manifest = verify(order_body(), sign(ORDER_ID), {"data.id": ORDER_ID})

        assert manifest is not None
        assert manifest.data_id == ORDER_ID

    def test_the_same_notification_signed_over_the_lowercased_id(self):
        assert verify(order_body(), sign(ORDER_ID.lower()), {"data.id": ORDER_ID}) is not None

    def test_a_signature_over_a_different_id_is_rejected(self):
        assert verify(order_body(), sign("ORDTST01OTHER"), {"data.id": ORDER_ID}) is None

    def test_a_numeric_id_still_verifies(self):
        body = json.dumps({"type": "payment", "data": {"id": "123456"}}).encode()

        assert verify(body, sign("123456"), {"data.id": "123456"}) is not None
        assert verify(body, sign("123456")) is not None

    def test_a_numeric_id_sent_as_a_json_number(self):
        body = json.dumps({"type": "payment", "data": {"id": 123456}}).encode()

        assert verify(body, sign("123456")) is not None

    def test_a_stale_ts_is_still_rejected(self):
        stale = str(int(time.time()) - 3600)

        assert verify(order_body(), sign(ORDER_ID, ts=stale), {"data.id": ORDER_ID}) is None

    def test_the_ledger_key_is_built_from_the_id_as_sent(self):
        ts = str(int(time.time()))

        manifest = verify(order_body(), sign(ORDER_ID.lower(), ts=ts), {"data.id": ORDER_ID})

        assert manifest is not None
        assert manifest.event_id == f"{ORDER_ID}:{REQUEST_ID}:{ts}"


class TestWhereTheIdComesFrom:
    def test_the_query_strings_id_is_used_when_present(self):
        """The body carries no id here, so only the URL's can have been signed."""
        manifest = verify(order_body(data_id=None), sign(ORDER_ID), {"data.id": ORDER_ID})

        assert manifest is not None
        assert manifest.data_id == ORDER_ID

    def test_the_bodys_id_is_used_without_a_query_string(self):
        assert verify(order_body(), sign(ORDER_ID)) is not None
        assert verify(order_body(), sign(ORDER_ID), {}) is not None

    def test_ids_that_differ_between_url_and_body_are_rejected(self):
        """Even when the URL's id is the one that was signed: the adapter acts on
        the body's."""
        body = order_body("ORDTST01ATTACKER")

        assert verify(body, sign(ORDER_ID), {"data.id": ORDER_ID}) is None

    def test_no_id_anywhere_is_rejected(self):
        assert verify(order_body(data_id=None), sign(ORDER_ID)) is None


class TestTheAdaptersPassTheQueryStringThrough:
    def test_verify_signature_and_get_event_id(self):
        adapter = MercadoPagoOrdersPaymentAdapter("APP_USR-token", webhook_secret=SECRET)
        body = order_body(data_id=None)
        headers = sign(ORDER_ID)
        query = {"data.id": ORDER_ID}

        assert adapter.verify_signature(body, headers, query_params=query) is True
        assert adapter.verify_signature(body, headers) is False
        assert adapter.get_event_id(body, headers, {}, query_params=query).startswith(ORDER_ID)


class TestAnAdapterWrittenBefore010_1:
    """A project adapter overriding ``verify_signature(raw_body, headers)`` is
    still called -- without the new keyword."""

    def test_query_params_reach_only_a_method_that_declares_them(self):
        def old(raw_body, headers): ...

        def new(raw_body, headers, *, query_params=None): ...

        def open_ended(raw_body, headers, **kwargs): ...

        query = {"data.id": "1"}
        assert payment_adapter_base.query_params_kwargs(old, query) == {}
        assert payment_adapter_base.query_params_kwargs(new, query) == {"query_params": query}
        assert payment_adapter_base.query_params_kwargs(open_ended, query) == {
            "query_params": query
        }
        assert payment_adapter_base.query_params_kwargs(new, None) == {}


@pytest.mark.parametrize("secret", ["", None])
def test_no_secret_rejects(secret):
    assert verify_mercadopago_signature(order_body(), sign(ORDER_ID), secret or "") is None
