import datetime
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from vinta_billing.constants import PaymentStatuses, RefundStatuses
from vinta_billing.exceptions import ProviderRequestRejectedError
from vinta_billing.services.dataclasses import Refund
from vinta_billing.services.payment_adapters import get_payment_provider_registry
from vinta_billing.services.payment_adapters.base import Payment
from vinta_billing.services.payment_adapters.mercadopago_orders_payment_adapter import (
    ORDER_REFUND_STATUS_MAPPING,
    ORDER_STATUS_DETAIL_MAPPING,
    ORDER_STATUS_MAPPING,
    MercadoPagoOrdersPaymentAdapter,
    iso8601_duration,
)
from vinta_billing.services.payment_adapters.mercadopago_payment_adapter import (
    MercadoPagoPaymentAdapter,
)


MODULE = "vinta_billing.services.payment_adapters.mercadopago_orders_payment_adapter"
NOW = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def mock_payment():
    address = Mock(
        street_name="Test Street",
        street_number="123",
        neighborhood="",
        city="Test City",
        state="SP",
        country="BR",
        zip_code="12345-678",
    )
    profile = Mock(
        email="buyer@example.com",
        document_type="CPF",
        document_number="12345678909",
        first_name="APRO",
        last_name="Doe",
        billing_address=address,
    )
    payment = Mock(spec=Payment)
    payment.id = 7
    payment.value = Decimal("150")
    payment.description = "Course"
    payment.payment_method = "visa"
    payment.billing_profile = profile
    payment.external_id = "ORD01ABC"
    return payment


@pytest.fixture
def mock_refund(mock_payment):
    refund = Mock(spec=Refund)
    refund.id = 3
    refund.value = Decimal("150.00")
    refund.payment = mock_payment
    refund.idempotency_key = ""
    refund.external_id = None
    return refund


@pytest.fixture
def adapter():
    with patch(
        "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoOrdersPaymentAdapter("APP_USR-test-token")
    adapter.sdk = mock_sdk.return_value
    return adapter


@pytest.fixture
def request_options():
    with patch(f"{MODULE}.mercadopago.config.RequestOptions") as mock_request_options:
        yield mock_request_options.return_value


def order(status="processed", status_detail="accredited", **payment):
    return {
        "id": "ORD01ABC",
        "status": status,
        "status_detail": status_detail,
        "total_amount": "150.00",
        "transactions": {
            "payments": [
                {
                    "id": "PAY01XYZ",
                    "amount": "150.00",
                    "status": status,
                    "status_detail": status_detail,
                    **payment,
                }
            ]
        },
    }


# --- Charges ---


def test_an_approved_card_charge(adapter, mock_payment, request_options):
    adapter.sdk.order().create.return_value = {"status": 201, "response": order()}

    result = adapter.process_charge(mock_payment, "card-token", idempotency_key="idem-1")

    body = adapter.sdk.order().create.call_args.args[0]
    assert body == {
        "type": "online",
        "processing_mode": "automatic",
        "total_amount": "150.00",
        "external_reference": "7",
        "description": "Course",
        "payer": {
            "email": "buyer@example.com",
            "first_name": "APRO",
            "last_name": "Doe",
            "identification": {"type": "CPF", "number": "12345678909"},
            "address": {
                "zip_code": "12345-678",
                "street_name": "Test Street",
                "street_number": "123",
                "city": "Test City",
                "state": "SP",
            },
        },
        "transactions": {
            "payments": [
                {
                    "amount": "150.00",
                    "payment_method": {
                        "id": "visa",
                        "type": "credit_card",
                        "token": "card-token",
                        "installments": 1,
                    },
                }
            ]
        },
    }
    assert request_options.custom_headers == {"x-idempotency-key": "idem-1"}
    assert result.external_id == "ORD01ABC"
    assert result.status == PaymentStatuses.APPROVED
    assert result.original_status == "processed"
    assert result.provider_data == {"transaction_id": "PAY01XYZ"}


def test_the_idempotency_key_falls_back_to_the_payment_id(adapter, mock_payment, request_options):
    adapter.sdk.order().create.return_value = {"status": 201, "response": order()}

    adapter.process_charge(mock_payment, "card-token")

    assert request_options.custom_headers == {"x-idempotency-key": "7"}


def test_a_refused_card_answering_402_is_a_declined_charge(adapter, mock_payment):
    adapter.sdk.order().create.return_value = {
        "status": 402,
        "response": {
            "errors": [
                {
                    "code": "failed",
                    "message": "The following transactions failed",
                    "details": ["PAY01XYZ: rejected_other_reason"],
                }
            ],
            "data": order("failed", "failed"),
        },
    }

    result = adapter.process_charge(mock_payment, "card-token")

    assert result.status == PaymentStatuses.REJECTED
    assert result.external_id == "ORD01ABC"
    assert result.original_status == "failed"


def test_a_402_without_the_order_is_still_a_declined_charge(adapter, mock_payment):
    adapter.sdk.order().create.return_value = {
        "status": 402,
        "response": {"errors": [{"code": "failed", "message": "transactions failed"}]},
    }

    result = adapter.process_charge(mock_payment, "card-token")

    assert result.status == PaymentStatuses.REJECTED
    assert result.external_id == ""


def test_another_4xx_raises_with_the_providers_codes(adapter, mock_payment):
    adapter.sdk.order().create.return_value = {
        "status": 400,
        "response": {
            "errors": [{"code": "invalid_total_amount", "message": "total_amount is invalid"}]
        },
    }

    with pytest.raises(ProviderRequestRejectedError) as excinfo:
        adapter.process_charge(mock_payment, "card-token")

    assert excinfo.value.http_status == 400
    assert excinfo.value.causes == (("invalid_total_amount", "total_amount is invalid"),)


@patch(f"{MODULE}.timezone.now", return_value=NOW)
def test_a_pix_charge(mock_now, adapter, mock_payment):
    mock_payment.payment_method = "pix"
    adapter.sdk.order().create.return_value = {
        "status": 201,
        "response": order(
            "action_required",
            "waiting_payment",
            date_of_expiration="2026-10-21T09:30:00.000-03:00",
            payment_method={
                "id": "pix",
                "type": "bank_transfer",
                "qr_code": "00020126580014br.gov.bcb.pix-synthetic",
                "qr_code_base64": "iVBORw0KGgo-synthetic",
                "ticket_url": "https://www.mercadopago.com.br/sandbox/payments/1/ticket",
            },
        ),
    }

    result = adapter.process_charge(
        mock_payment, "", expires_at=NOW + datetime.timedelta(minutes=30)
    )

    body = adapter.sdk.order().create.call_args.args[0]
    assert body["transactions"]["payments"][0]["payment_method"] == {
        "id": "pix",
        "type": "bank_transfer",
    }
    assert body["expiration_time"] == "PT30M"
    assert result.status == PaymentStatuses.PENDING
    assert result.provider_data == {
        "qr_code": "00020126580014br.gov.bcb.pix-synthetic",
        "qr_code_base64": "iVBORw0KGgo-synthetic",
        "ticket_url": "https://www.mercadopago.com.br/sandbox/payments/1/ticket",
        "transaction_id": "PAY01XYZ",
    }
    assert result.expires_at == NOW + datetime.timedelta(minutes=30)


def test_a_boleto_is_sent_as_a_ticket_without_a_token(adapter, mock_payment):
    mock_payment.payment_method = "boleto"
    adapter.sdk.order().create.return_value = {
        "status": 201,
        "response": order("action_required", "waiting_payment"),
    }

    adapter.process_charge(mock_payment, "")

    payment_method = adapter.sdk.order().create.call_args.args[0]["transactions"]["payments"][0][
        "payment_method"
    ]
    assert payment_method == {"id": "bolbradesco", "type": "ticket"}


def test_expires_at_must_be_aware_and_in_the_future(adapter, mock_payment):
    with pytest.raises(ValueError, match="timezone-aware"):
        adapter.process_charge(mock_payment, "", expires_at=datetime.datetime(2030, 1, 1))
    with pytest.raises(ValueError, match="future"):
        adapter.process_charge(
            mock_payment, "", expires_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        )


@pytest.mark.parametrize(
    ("delta", "duration"),
    [
        (datetime.timedelta(minutes=30), "PT30M"),
        (datetime.timedelta(hours=2), "PT2H"),
        (datetime.timedelta(hours=1, minutes=1, seconds=5), "PT1H1M5S"),
        (datetime.timedelta(seconds=0), "PT0S"),
    ],
)
def test_iso8601_duration(delta, duration):
    assert iso8601_duration(delta) == duration


# --- Status ---


@pytest.mark.parametrize(
    ("status", "status_detail", "expected"),
    [
        ("processed", "accredited", PaymentStatuses.APPROVED),
        ("processed", "partially_refunded", PaymentStatuses.PARTIALLY_REFUNDED),
        ("action_required", "waiting_payment", PaymentStatuses.PENDING),
        ("processing", "in_process", PaymentStatuses.IN_PROCESS),
        ("failed", "failed", PaymentStatuses.REJECTED),
        ("failed", "expired", PaymentStatuses.EXPIRED),
        ("canceled", "expired", PaymentStatuses.EXPIRED),
        ("canceled", "canceled", PaymentStatuses.CANCELLED),
        ("refunded", "refunded", PaymentStatuses.REFUNDED),
        ("charged_back", "in_process", PaymentStatuses.IN_MEDIATION),
        ("charged_back", "settled", PaymentStatuses.CHARGED_BACK),
        ("something_new", "x", PaymentStatuses.UNKNOWN),
    ],
)
def test_check_status_reads_the_order(adapter, status, status_detail, expected):
    adapter.sdk.order().get.return_value = {"status": 200, "response": order(status, status_detail)}

    result = adapter.check_status("ORD01ABC", update_id="unsigned-id")

    adapter.sdk.order().get.assert_called_once_with("ORD01ABC")
    assert result.status == expected
    assert result.original_status == status
    assert result.description == status_detail
    assert result.update_external_id == "ORD01ABC"


def test_check_status_raises_on_an_error_answer(adapter):
    adapter.sdk.order().get.return_value = {
        "status": 404,
        "response": {"errors": [{"code": "order_not_found", "message": "Order not found"}]},
    }

    with pytest.raises(ProviderRequestRejectedError):
        adapter.check_status("ORD-missing")


@pytest.mark.parametrize(
    "mapped",
    [
        *ORDER_STATUS_MAPPING.values(),
        *ORDER_STATUS_DETAIL_MAPPING.values(),
    ],
)
def test_order_status_mapping_values_are_payment_statuses(mapped):
    assert mapped in PaymentStatuses.values


@pytest.mark.parametrize("mapped", ORDER_REFUND_STATUS_MAPPING.values())
def test_order_refund_status_mapping_values_are_refund_statuses(mapped):
    assert mapped in RefundStatuses.values


# --- Refunds ---


def refunded_order(*refunds):
    answer = order("refunded", "refunded")
    answer["transactions"]["refunds"] = list(refunds)
    return answer


def test_a_full_refund_has_no_body(adapter, mock_refund, request_options):
    adapter.sdk.order().get.return_value = {"status": 200, "response": order()}
    adapter.sdk.order().refund_transaction.return_value = {
        "status": 201,
        "response": refunded_order(
            {"id": "REF01", "transaction_id": "PAY01XYZ", "amount": "150.00", "status": "processed"}
        ),
    }

    result = adapter.refund(mock_refund)

    adapter.sdk.order().refund_transaction.assert_called_once_with(
        "ORD01ABC", None, request_options
    )
    assert request_options.custom_headers == {"x-idempotency-key": "3"}
    assert result.external_id == "REF01"
    assert result.status == RefundStatuses.APPROVED


def test_a_partial_refund_names_the_transaction_and_amount(adapter, mock_refund, request_options):
    mock_refund.value = Decimal("50")
    mock_refund.idempotency_key = "refund-key"
    adapter.sdk.order().get.return_value = {"status": 200, "response": order()}
    adapter.sdk.order().refund_transaction.return_value = {
        "status": 201,
        "response": refunded_order(
            {"id": "REF01", "transaction_id": "PAY01XYZ", "amount": "50.00", "status": "processing"}
        ),
    }

    result = adapter.refund(mock_refund)

    adapter.sdk.order().refund_transaction.assert_called_once_with(
        "ORD01ABC",
        {"transactions": [{"id": "PAY01XYZ", "amount": "50.00"}]},
        request_options,
    )
    assert request_options.custom_headers == {"x-idempotency-key": "refund-key"}
    assert result.status == RefundStatuses.PENDING


def test_refunding_the_rest_after_a_partial_refund_is_partial(adapter, mock_refund):
    """With something already refunded, a body-less refund would not mean
    "this amount", so the rest is refunded as a partial one."""
    mock_refund.value = Decimal("100.00")
    already = order("processed", "partially_refunded")
    already["transactions"]["refunds"] = [{"id": "REF01", "amount": "50.00", "status": "processed"}]
    adapter.sdk.order().get.return_value = {"status": 200, "response": already}
    adapter.sdk.order().refund_transaction.return_value = {
        "status": 201,
        "response": refunded_order(
            {"id": "REF01", "amount": "50.00", "status": "processed"},
            {"id": "REF02", "amount": "100.00", "status": "processed"},
        ),
    }

    result = adapter.refund(mock_refund)

    assert adapter.sdk.order().refund_transaction.call_args.args[1] == {
        "transactions": [{"id": "PAY01XYZ", "amount": "100.00"}]
    }
    assert result.external_id == "REF02"


def test_refund_raises_on_an_error_answer(adapter, mock_refund):
    adapter.sdk.order().get.return_value = {"status": 200, "response": order()}
    adapter.sdk.order().refund_transaction.return_value = {
        "status": 409,
        "response": {"errors": [{"code": "order_already_refunded", "message": "already"}]},
    }

    with pytest.raises(ProviderRequestRejectedError):
        adapter.refund(mock_refund)


def test_check_refund_status_reads_the_order(adapter, mock_refund):
    mock_refund.external_id = "REF02"
    adapter.sdk.order().get.return_value = {
        "status": 200,
        "response": refunded_order(
            {"id": "REF01", "status": "processing"}, {"id": "REF02", "status": "processed"}
        ),
    }

    assert adapter.check_refund_status(mock_refund) == RefundStatuses.APPROVED
    adapter.sdk.order().get.assert_called_once_with("ORD01ABC")


def test_check_refund_status_without_the_refund_on_the_order(adapter, mock_refund):
    mock_refund.external_id = "REF09"
    adapter.sdk.order().get.return_value = {"status": 200, "response": refunded_order()}

    assert adapter.check_refund_status(mock_refund) == RefundStatuses.UNKNOWN


def test_check_refund_status_without_an_external_id(adapter, mock_refund):
    assert adapter.check_refund_status(mock_refund) == RefundStatuses.UNKNOWN
    adapter.sdk.order().get.assert_not_called()


# --- Webhooks ---


@pytest.mark.parametrize("action", ["order.processed", "order.refunded", "order.canceled"])
def test_an_order_notification_is_read_off_the_api(adapter, action):
    adapter.sdk.order().get.return_value = {"status": 200, "response": order()}

    result = adapter.receive_update(
        {"type": "order", "action": action, "data": {"id": "ORD01ABC"}, "id": "notif-1"}
    )

    assert result is not None
    external_id, update = result
    assert external_id == "ORD01ABC"
    assert update.status == PaymentStatuses.APPROVED
    adapter.sdk.order().get.assert_called_once_with("ORD01ABC")


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "payment", "action": "payment.update", "data": {"id": "123"}},
        {"type": "order", "action": "payment.update", "data": {"id": "ORD01ABC"}},
    ],
)
def test_a_non_order_notification_is_ignored(adapter, payload):
    assert adapter.receive_update(payload) is None
    adapter.sdk.order().get.assert_not_called()


# --- The `API` setting ---


def test_payments_is_the_default_api():
    adapter = MercadoPagoPaymentAdapter.from_config({"ACCESS_TOKEN": "TEST-token"})

    assert type(adapter) is MercadoPagoPaymentAdapter


def test_api_payments_builds_the_payments_adapter():
    adapter = MercadoPagoPaymentAdapter.from_config({"ACCESS_TOKEN": "t", "API": "payments"})

    assert type(adapter) is MercadoPagoPaymentAdapter


def test_api_orders_builds_the_orders_adapter():
    adapter = MercadoPagoPaymentAdapter.from_config(
        {
            "ACCESS_TOKEN": "APP_USR-token",
            "WEBHOOK_SECRET": "secret",
            "NOTIFICATION_URL_HOST": "api.example.com",
            "API": "Orders",
        }
    )

    assert type(adapter) is MercadoPagoOrdersPaymentAdapter
    assert adapter.access_token == "APP_USR-token"
    assert adapter.webhook_secret == "secret"
    assert adapter.is_configured is True


def test_an_unknown_api_is_refused():
    with pytest.raises(ImproperlyConfigured, match="'checkout'"):
        MercadoPagoPaymentAdapter.from_config({"ACCESS_TOKEN": "t", "API": "checkout"})


def test_the_registry_honours_the_api_setting():
    with override_settings(
        VINTA_BILLING={"PROVIDERS": {"mercadopago": {"ACCESS_TOKEN": "t", "API": "orders"}}}
    ):
        assert type(get_payment_provider_registry()["mercadopago"]) is (
            MercadoPagoOrdersPaymentAdapter
        )
    with override_settings(VINTA_BILLING={"PROVIDERS": {"mercadopago": {"ACCESS_TOKEN": "t"}}}):
        assert type(get_payment_provider_registry()["mercadopago"]) is MercadoPagoPaymentAdapter
