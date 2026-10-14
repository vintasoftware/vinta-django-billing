"""A signed MercadoPago payment webhook, end to end.

Through the real URL, the real view, the real signature check and the real
idempotency ledger -- only the MercadoPago SDK is mocked, so nothing leaves the
process. What it proves is the contract a project builds on:
``Payment.status`` follows the delivery, ``payment_status_changed`` is received
exactly once and only after the transaction committed, and replaying the same
delivery changes and sends nothing.

``transaction=True`` on purpose: the ordinary test transaction never commits, so
"after commit" could only be simulated there. Here the receiver checks for
itself that it runs outside any transaction and can already read the new status.
"""

import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.db import connection
from django.test import override_settings

from vinta_billing.constants import PaymentProviders, PaymentStatuses
from vinta_billing.models import Payment, PaymentStatusUpdate, ProviderWebhookEvent
from vinta_billing.signals import payment_status_changed


WEBHOOK_SECRET = "synthetic-webhook-secret"
PROVIDERS = {
    PaymentProviders.MERCADOPAGO: {
        "ACCESS_TOKEN": "synthetic-access-token",
        "WEBHOOK_SECRET": WEBHOOK_SECRET,
    }
}


def signed_delivery(data_id: str, request_id: str = "req-1") -> tuple[bytes, dict[str, str]]:
    """A body and headers signed the way MercadoPago signs a payment notification."""
    ts = str(int(time.time()))
    body = json.dumps(
        {"id": 987, "type": "payment", "action": "payment.update", "data": {"id": data_id}}
    ).encode()
    manifest = f"id:{data_id.lower()};request-id:{request_id};ts:{ts};"
    signature = hmac.new(WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return body, {"x-signature": f"ts={ts},v1={signature}", "x-request-id": request_id}


@pytest.fixture
def mercadopago_sdk():
    """The SDK every adapter built during the test talks to."""
    with patch(
        "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.SDK"
    ) as sdk_class:
        sdk = MagicMock()
        sdk_class.return_value = sdk
        yield sdk


@pytest.fixture
def payment(billing_profile):
    return Payment.objects.create(
        billing_profile=billing_profile,
        currency="BRL",
        value=Decimal("49.90"),
        description="Synthetic charge",
        payment_method="pix",
        status=PaymentStatuses.PENDING,
        payment_provider=PaymentProviders.MERCADOPAGO,
        external_id="555001",
    )


@pytest.fixture
def received():
    calls: list[dict] = []

    def receiver(sender, payment, from_status, to_status, **kwargs):
        calls.append(
            {
                "payment_id": payment.pk,
                "from_status": from_status,
                "to_status": to_status,
                "in_transaction": connection.in_atomic_block,
                "stored_status": Payment.objects.get(pk=payment.pk).status,
            }
        )

    payment_status_changed.connect(receiver)
    yield calls
    payment_status_changed.disconnect(receiver)


@pytest.mark.django_db(transaction=True)
@override_settings(VINTA_BILLING={"PROVIDERS": PROVIDERS})
def test_signed_webhook_updates_the_payment_and_signals_once_after_commit(
    client, payment, mercadopago_sdk, received
):
    mercadopago_sdk.payment().get.return_value = {
        "response": {"id": "555001", "status": "approved", "status_detail": "accredited"}
    }
    body, headers = signed_delivery("555001")
    url = f"/api/billing/payments/{payment.pk}/payment-update/mercadopago/"

    response = client.post(url, data=body, content_type="application/json", headers=headers)

    assert response.status_code == 200
    payment.refresh_from_db()
    assert payment.status == PaymentStatuses.APPROVED
    assert payment.original_status == "approved"
    assert received == [
        {
            "payment_id": payment.pk,
            "from_status": PaymentStatuses.PENDING,
            "to_status": PaymentStatuses.APPROVED,
            "in_transaction": False,
            "stored_status": PaymentStatuses.APPROVED,
        }
    ]

    # The same delivery again: the ledger recognises it, nothing is written or sent.
    replay = client.post(url, data=body, content_type="application/json", headers=headers)

    assert replay.status_code == 200
    assert len(received) == 1
    assert PaymentStatusUpdate.objects.filter(payment=payment).count() == 1
    assert ProviderWebhookEvent.objects.count() == 1


@pytest.mark.django_db(transaction=True)
@override_settings(VINTA_BILLING={"PROVIDERS": PROVIDERS})
def test_a_second_notification_with_the_same_status_sends_nothing(
    client, payment, mercadopago_sdk, received
):
    """MercadoPago notifies on any change to a payment, not only its status: a
    *different* delivery reporting the status already recorded is history, not a
    change."""
    mercadopago_sdk.payment().get.return_value = {
        "response": {"id": "555001", "status": "approved", "status_detail": "accredited"}
    }
    url = f"/api/billing/payments/{payment.pk}/payment-update/mercadopago/"

    for request_id in ("req-1", "req-2"):
        body, headers = signed_delivery("555001", request_id=request_id)
        response = client.post(url, data=body, content_type="application/json", headers=headers)
        assert response.status_code == 200

    assert len(received) == 1
    assert PaymentStatusUpdate.objects.filter(payment=payment).count() == 2


@pytest.mark.django_db(transaction=True)
@override_settings(VINTA_BILLING={"PROVIDERS": PROVIDERS})
def test_an_expired_pix_charge_becomes_expired(client, payment, mercadopago_sdk, received):
    mercadopago_sdk.payment().get.return_value = {
        "response": {"id": "555001", "status": "cancelled", "status_detail": "expired"}
    }
    body, headers = signed_delivery("555001")

    client.post(
        f"/api/billing/payments/{payment.pk}/payment-update/mercadopago/",
        data=body,
        content_type="application/json",
        headers=headers,
    )

    payment.refresh_from_db()
    assert payment.status == PaymentStatuses.EXPIRED
    assert payment.original_status == "cancelled"
    assert [c["to_status"] for c in received] == [PaymentStatuses.EXPIRED]
