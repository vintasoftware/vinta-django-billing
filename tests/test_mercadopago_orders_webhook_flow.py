"""A signed MercadoPago Orders API notification, end to end.

The request is shaped after the one MercadoPago's "Simular notificação" sent
(topic ``order``): an uppercase ``ORD...`` id signed as sent, ``data.id`` in the
query string as well as the body, and the query string HTML-escaped
(``&amp;type=order``). Signed with a throwaway key generated per run; only the
SDK is mocked.
"""

import hashlib
import hmac
import json
import secrets
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from django.urls import reverse

from vinta_billing.constants import PaymentProviders, PaymentStatuses
from vinta_billing.models import Payment


SIGNING_KEY = secrets.token_hex(16)
ORDER_ID = "ORDTST01M5EPFPG2SRJTVMQ3JEK47XZN"
PROVIDERS = {
    PaymentProviders.MERCADOPAGO: {
        "ACCESS_TOKEN": "APP_USR-synthetic",
        "WEBHOOK_SECRET": SIGNING_KEY,
        "API": "orders",
    }
}


def signed_order_notification(signed_id: str = ORDER_ID) -> tuple[bytes, dict[str, str]]:
    ts = str(int(time.time()))
    request_id = "e643f528-a721-44f5-9fc3-9ce98961920d"
    body = json.dumps(
        {
            "action": "order.processed",
            "api_version": "v1",
            "data": {"id": ORDER_ID},
            "type": "order",
        }
    ).encode()
    manifest = f"id:{signed_id};request-id:{request_id};ts:{ts};"
    v1 = hmac.new(SIGNING_KEY.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return body, {"x-signature": f"ts={ts},v1={v1}", "x-request-id": request_id}


@pytest.fixture
def mercadopago_sdk():
    with patch(
        "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.SDK"
    ) as sdk_class:
        sdk = MagicMock()
        sdk_class.return_value = sdk
        sdk.order().get.return_value = {
            "status": 200,
            "response": {"id": ORDER_ID, "status": "processed", "status_detail": "accredited"},
        }
        yield sdk


@pytest.fixture
def pix_payment(billing_profile):
    return Payment.objects.create(
        billing_profile=billing_profile,
        currency="BRL",
        value=Decimal("150.00"),
        description="Course",
        payment_method="pix",
        status=PaymentStatuses.PENDING,
        payment_provider=PaymentProviders.MERCADOPAGO,
        external_id=ORDER_ID,
    )


def provider_route() -> str:
    return reverse(
        "billing:Payments-provider-payment-update",
        kwargs={"provider": PaymentProviders.MERCADOPAGO},
    )


def per_payment_route(payment) -> str:
    return reverse(
        "billing:Payments-payment-update",
        kwargs={"provider": PaymentProviders.MERCADOPAGO, "pk": payment.pk},
    )


def post(client, url, body, headers):
    return client.post(url, data=body, content_type="application/json", headers=headers)


@pytest.mark.django_db
class TestOrderNotification:
    @pytest.fixture(autouse=True)
    def orders_api(self):
        with override_settings(VINTA_BILLING={"PROVIDERS": PROVIDERS}):
            yield

    def test_the_provider_route_has_no_payment_id(self):
        assert provider_route() == "/api/billing/payments/payment-update/mercadopago/"

    @pytest.mark.parametrize("route", ["provider", "per-payment"])
    def test_an_order_notification_updates_the_payment(
        self, client, mercadopago_sdk, pix_payment, route
    ):
        body, headers = signed_order_notification()
        path = provider_route() if route == "provider" else per_payment_route(pix_payment)

        response = post(client, f"{path}?data.id={ORDER_ID}&amp;type=order", body, headers)

        assert response.status_code == 200
        pix_payment.refresh_from_db()
        assert pix_payment.status == PaymentStatuses.APPROVED
        mercadopago_sdk.order().get.assert_called_with(ORDER_ID)

    def test_signed_over_the_lowercased_id_is_accepted_too(
        self, client, mercadopago_sdk, pix_payment
    ):
        body, headers = signed_order_notification(ORDER_ID.lower())

        response = post(client, f"{provider_route()}?data.id={ORDER_ID}&type=order", body, headers)

        assert response.status_code == 200

    def test_an_escaped_ampersand_before_data_id_still_finds_it(
        self, client, mercadopago_sdk, pix_payment
    ):
        body, headers = signed_order_notification()

        response = post(
            client, f"{provider_route()}?type=order&amp;data.id={ORDER_ID}", body, headers
        )

        assert response.status_code == 200

    def test_a_url_id_differing_from_the_body_is_refused(
        self, client, mercadopago_sdk, pix_payment
    ):
        body, headers = signed_order_notification()

        response = post(
            client, f"{provider_route()}?data.id=ORDTST01OTHER&type=order", body, headers
        )

        assert response.status_code == 403
        pix_payment.refresh_from_db()
        assert pix_payment.status == PaymentStatuses.PENDING

    def test_a_signature_over_another_id_is_refused(self, client, mercadopago_sdk, pix_payment):
        body, headers = signed_order_notification("ORDTST01OTHER")

        assert post(client, provider_route(), body, headers).status_code == 403
