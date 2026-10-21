import datetime
import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from vinta_billing.constants import PaymentProviders, PaymentStatuses, RefundStatuses
from vinta_billing.exceptions import (
    ProviderRequestRejectedError,
    ProviderWebhookEventIdMissingError,
)
from vinta_billing.services.dataclasses import Refund, RefundResult
from vinta_billing.services.payment_adapters.base import Payment, PaymentStatusUpdate
from vinta_billing.services.payment_adapters.mercadopago_payment_adapter import (
    PAYMENT_STATUS_MAPPING,
    REFUND_STATUS_MAPPING,
    MercadoPagoPaymentAdapter,
)


WEBHOOK_SECRET = "test-webhook-secret"


def build_signed_request(
    data_id: str,
    secret: str = WEBHOOK_SECRET,
    request_id: str = "req-123",
    ts: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Build a raw body + headers pair signed the way MercadoPago signs webhooks.

    ``ts`` defaults to "now" — the signature tolerance window rejects a stale
    ``ts`` (see ``test_verify_signature_rejects_stale_timestamp``), so tests that
    aren't specifically exercising that behavior must sign with a fresh timestamp.
    """
    if ts is None:
        ts = str(int(time.time()))
    raw_body = json.dumps({"action": "payment.update", "data": {"id": data_id}}).encode()
    manifest = f"id:{data_id.lower()};request-id:{request_id};ts:{ts};"
    signature = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    headers = {
        "x-signature": f"ts={ts},v1={signature}",
        "x-request-id": request_id,
    }
    return raw_body, headers


@pytest.fixture
def mock_billing_address():
    """Mock billing address object."""
    address = Mock()
    address.street_name = "Test Street"
    address.street_number = "123"
    address.neighborhood = "Test Neighborhood"
    address.city = "Test City"
    address.state = "Test State"
    address.country = "BR"
    address.zip_code = "12345-678"
    return address


@pytest.fixture
def mock_billing_profile(mock_billing_address):
    """Mock billing profile object."""
    profile = Mock()
    profile.email = "test@example.com"
    profile.document_type = "CPF"
    profile.document_number = "12345678901"
    profile.first_name = "John"
    profile.last_name = "Doe"
    profile.billing_address = mock_billing_address
    return profile


@pytest.fixture
def mock_payment(mock_billing_profile):
    """Mock payment object."""
    payment = Mock(spec=Payment)
    payment.id = "payment-123"
    payment.value = Decimal("100.50")
    payment.description = "Test payment"
    payment.payment_method = "credit_card"
    payment.billing_profile = mock_billing_profile
    payment.external_id = "mp-payment-456"
    return payment


@pytest.fixture
def mock_refund(mock_payment):
    """Mock refund object."""
    refund = Mock(spec=Refund)
    refund.id = "refund-123"
    refund.value = Decimal("50.25")
    refund.payment = mock_payment
    refund.idempotency_key = ""
    return refund


@pytest.fixture
def adapter():
    """Create MercadoPagoAdapter instance with mocked SDK."""
    with patch(
        "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoPaymentAdapter("test-access-token", webhook_secret=WEBHOOK_SECRET)
        adapter.sdk = mock_sdk.return_value
        return adapter


def test_init():
    """Test adapter initialization."""
    with patch(
        "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoPaymentAdapter("test-token")
        mock_sdk.assert_called_once_with("test-token")
        assert adapter.provider == PaymentProviders.MERCADOPAGO


@patch("vinta_billing.services.payment_adapters.mercadopago_payment_adapter.reverse")
@patch(
    "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.config.RequestOptions"
)
@override_settings(SITE_DOMAIN="example.com")
def test_process_success(mock_request_options, mock_reverse, adapter, mock_payment):
    """Test successful payment processing."""
    # Setup mocks
    mock_reverse.return_value = "/payment/update/mercadopago/payment-123/"
    mock_options = Mock()
    mock_request_options.return_value = mock_options

    adapter.sdk.payment().create.return_value = {"response": {"id": "mp-payment-456"}}

    # Execute
    result = adapter.process(mock_payment, "test-token")

    # Verify
    assert result == "mp-payment-456"
    mock_options.custom_headers = {"x-idempotency-key": {"payment-123"}}

    expected_payment_data = {
        "transaction_amount": 100.5,
        "token": "test-token",
        "description": "Test payment",
        "payment_method_id": "credit_card",
        "notification_url": "https://example.com/payment/update/mercadopago/payment-123/",
        "installments": 1,
        "payer": {
            "email": "test@example.com",
            "identification": {
                "type": "CPF",
                "number": "12345678901",
            },
            "first_name": "John",
            "last_name": "Doe",
            "address": {
                "street_name": "Test Street",
                "street_number": "123",
                "neighborhood": "Test Neighborhood",
                "city": "Test City",
                "federal_unit": "Test State",
                "zip_code": "12345-678",
            },
        },
    }
    adapter.sdk.payment().create.assert_called_once_with(expected_payment_data, mock_options)


@patch("vinta_billing.services.payment_adapters.mercadopago_payment_adapter.reverse")
@patch(
    "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.config.RequestOptions"
)
@override_settings(SITE_DOMAIN="example.com")
def test_process_forwards_idempotency_key_as_header(
    mock_request_options, mock_reverse, adapter, mock_payment
):
    """A client-supplied idempotency key must reach MercadoPago as the
    `x-idempotency-key` header so a retried charge is deduped provider-side. No
    live provider here -- assert the header value is threaded through."""
    mock_reverse.return_value = "/payment/update/mercadopago/payment-123/"
    mock_options = Mock()
    mock_request_options.return_value = mock_options
    adapter.sdk.payment().create.return_value = {"response": {"id": "mp-payment-456"}}

    adapter.process(mock_payment, "test-token", idempotency_key="idem-key-1")

    assert mock_options.custom_headers == {"x-idempotency-key": "idem-key-1"}


@patch("vinta_billing.services.payment_adapters.mercadopago_payment_adapter.reverse")
@patch(
    "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.config.RequestOptions"
)
@override_settings(SITE_DOMAIN="example.com")
def test_process_without_idempotency_key_falls_back_to_payment_id(
    mock_request_options, mock_reverse, adapter, mock_payment
):
    """Absent a caller key, the header falls back to the (stringified) local
    payment id -- a string, not the prior `{payment.id}` set literal."""
    mock_reverse.return_value = "/payment/update/mercadopago/payment-123/"
    mock_options = Mock()
    mock_request_options.return_value = mock_options
    adapter.sdk.payment().create.return_value = {"response": {"id": "mp-payment-456"}}

    adapter.process(mock_payment, "test-token")

    assert mock_options.custom_headers == {"x-idempotency-key": "payment-123"}


@override_settings(SITE_DOMAIN=None)
def test_process_missing_site_domain(adapter, mock_payment):
    """Test process raises error when SITE_DOMAIN is not configured."""
    with pytest.raises(ImproperlyConfigured, match="SITE_DOMAIN"):
        adapter.process(mock_payment, "test-token")


@patch(
    "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.config.RequestOptions"
)
def test_refund_success(mock_request_options, adapter, mock_refund):
    """Test successful refund processing.

    MercadoPago's create-refund response carries the refund's status
    synchronously alongside its id — the adapter returns both in one
    `RefundResult` rather than forcing a second `check_refund_status` round trip
    just to learn what this response already said.
    """
    # Setup mocks
    mock_options = Mock()
    mock_request_options.return_value = mock_options

    adapter.sdk.refund().create.return_value = {
        "response": {"id": "refund-456", "status": "approved"}
    }

    # Execute
    result = adapter.refund(mock_refund)

    # Verify
    assert result == RefundResult(external_id="refund-456", status=RefundStatuses.APPROVED)
    assert mock_options.custom_headers == {"x-idempotency-key": "refund-123"}
    adapter.sdk.refund().create.assert_called_once_with(
        "mp-payment-456", {"amount": 50.25}, mock_options
    )


@patch(
    "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.config.RequestOptions"
)
@patch("vinta_billing.services.payment_adapters.mercadopago_payment_adapter.logger")
def test_refund_unknown_status_logs_no_pii(mock_logger, mock_request_options, adapter, mock_refund):
    adapter.sdk.refund().create.return_value = {
        "response": {"id": "refund-456", "status": "some_new_mp_status"}
    }

    result = adapter.refund(mock_refund)

    assert result.status == RefundStatuses.UNKNOWN
    mock_logger.error.assert_called_once_with(
        "Unknown refund status: refund_id=%s original_status=%s",
        mock_refund.id,
        "some_new_mp_status",
    )


def test_check_status(adapter):
    """Test payment status checking.

    `update_external_id` is sourced from the authenticated API response's own
    `id`, not the (unsigned, caller-supplied) `update_id` argument — see
    `test_check_status_ignores_unsigned_update_id_argument`.
    """
    # Setup mock
    adapter.sdk.payment().get.return_value = {
        "response": {
            "id": "mp-payment-456",
            "status": "approved",
            "status_detail": "accredited",
        }
    }

    # Execute
    result = adapter.check_status("mp-payment-456")

    # Verify
    assert isinstance(result, PaymentStatusUpdate)
    assert result.id is None
    assert result.status == "approved"
    assert result.description == "accredited"
    assert result.update_external_id == "mp-payment-456"
    adapter.sdk.payment().get.assert_called_once_with("mp-payment-456")


def test_check_status_ignores_unsigned_update_id_argument(adapter):
    """The `update_id` argument is never trusted for `update_external_id` — it
    would otherwise let an attacker replaying a captured valid signature stamp an
    arbitrary external id onto a `PaymentStatusUpdate` (see the module docstring on
    `MercadoPagoWebhookSignature`)."""
    adapter.sdk.payment().get.return_value = {
        "response": {
            "id": "mp-payment-456",
            "status": "approved",
            "status_detail": "accredited",
        }
    }

    result = adapter.check_status("mp-payment-456", update_id="attacker-supplied-id")

    assert result.update_external_id == "mp-payment-456"


def test_get_payment_external_id_from_update(adapter):
    """Test extracting payment external ID from update payload."""
    update_payload = {"data": {"id": "mp-payment-456"}, "other_field": "value"}

    result = adapter.get_payment_external_id_from_update(update_payload)
    assert result == "mp-payment-456"


def test_get_payment_external_id_from_update_missing_data(adapter):
    """Test extracting payment external ID when data is missing."""
    update_payload = {"other_field": "value"}

    result = adapter.get_payment_external_id_from_update(update_payload)
    assert result is None


def test_get_update_id(adapter):
    """Test extracting update ID from payload."""
    update_payload = {"id": "update-123", "other_field": "value"}

    result = adapter.get_update_id(update_payload)
    assert result == "update-123"


def test_get_update_id_missing(adapter):
    """Test extracting update ID when missing."""
    update_payload = {"other_field": "value"}

    result = adapter.get_update_id(update_payload)
    assert result is None


@patch.object(MercadoPagoPaymentAdapter, "check_status")
def test_receive_update_valid_payment_update(mock_check_status, adapter):
    """Test receiving valid payment update."""
    update_payload = {
        "type": "payment",
        "action": "payment.update",
        "data": {"id": "mp-payment-456"},
        "id": "update-123",
    }

    mock_status_update = PaymentStatusUpdate(
        id=None, status="approved", description="accredited", update_external_id="update-123"
    )
    mock_check_status.return_value = mock_status_update

    result = adapter.receive_update(update_payload)

    assert result == ("mp-payment-456", mock_status_update)
    # The payload's top-level "id" is unsigned and no longer threaded through to
    # `check_status` — see `test_check_status_ignores_unsigned_update_id_argument`.
    mock_check_status.assert_called_once_with("mp-payment-456")


def test_receive_update_invalid_type(adapter):
    """Test receiving update with invalid type."""
    update_payload = {
        "type": "refund",
        "action": "payment.update",
        "data": {"id": "mp-payment-456"},
    }

    result = adapter.receive_update(update_payload)
    assert result is None


def test_receive_update_invalid_action(adapter):
    """Test receiving update with invalid action."""
    update_payload = {
        "type": "payment",
        "action": "payment.created",
        "data": {"id": "mp-payment-456"},
    }

    result = adapter.receive_update(update_payload)
    assert result is None


def test_check_refund_status_success(adapter, mock_refund):
    """MercadoPago has no single-refund-by-id lookup — only "list refunds for a
    payment" — so this reads the parent payment's external id off
    `refund.payment.external_id`, not just the refund's own id."""
    mock_refund.external_id = "refund-456"
    adapter.sdk.refund().list_all.return_value = {
        "response": [{"id": "refund-456", "status": "approved"}]
    }

    result = adapter.check_refund_status(mock_refund)

    assert result == RefundStatuses.APPROVED
    adapter.sdk.refund().list_all.assert_called_once_with("mp-payment-456")


@patch("vinta_billing.services.payment_adapters.mercadopago_payment_adapter.logger")
def test_check_refund_status_unknown(mock_logger, adapter, mock_refund):
    """Test refund status check with unknown status."""
    mock_refund.external_id = "refund-456"
    adapter.sdk.refund().list_all.return_value = {
        "response": [{"id": "refund-456", "status": "unknown_status"}]
    }

    result = adapter.check_refund_status(mock_refund)

    assert result == RefundStatuses.UNKNOWN
    # Logs the id + status only — never the raw list response, which would leak
    # payer PII once the payload carries a real MercadoPago response.
    mock_logger.error.assert_called_once_with(
        "Unknown refund status: refund_external_id=%s original_status=%s",
        "refund-456",
        "unknown_status",
    )


def test_check_refund_status_not_found_in_list(adapter, mock_refund):
    """A refund id absent from the provider's own list must not be silently
    reported as some default status."""
    mock_refund.external_id = "refund-456"
    adapter.sdk.refund().list_all.return_value = {"response": []}

    result = adapter.check_refund_status(mock_refund)

    assert result == RefundStatuses.UNKNOWN


def test_check_refund_status_without_external_id(adapter, mock_refund):
    """Cannot poll a refund that was never assigned a provider id."""
    mock_refund.external_id = None

    result = adapter.check_refund_status(mock_refund)

    assert result == RefundStatuses.UNKNOWN


def test_payment_methods_mapping_usage(adapter, mock_payment):
    """Test that payment methods mapping is used when available."""
    mock_payment.payment_method = "visa"

    with patch.dict(
        "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.PAYMENT_METHODS_MAPPING",
        {"visa": "visa_card"},
    ):
        with override_settings(SITE_DOMAIN="example.com"):
            with patch(
                "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.reverse"
            ):
                with patch(
                    "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.config.RequestOptions"
                ):
                    adapter.sdk.payment().create.return_value = {"response": {"id": "test-id"}}

                    adapter.process(mock_payment, "test-token")

                    # Verify the mapped payment method was used
                    call_args = adapter.sdk.payment().create.call_args[0][0]
                    assert call_args["payment_method_id"] == "visa_card"


def test_document_types_mapping_usage(adapter, mock_payment):
    """Test that document types mapping is used when available."""
    mock_payment.billing_profile.document_type = "CPF"

    with patch.dict(
        "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.DOCUMENT_TYPES_MAPPING",
        {"CPF": "cpf_mapped"},
    ):
        with override_settings(SITE_DOMAIN="example.com"):
            with patch(
                "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.reverse"
            ):
                with patch(
                    "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.config.RequestOptions"
                ):
                    adapter.sdk.payment().create.return_value = {"response": {"id": "test-id"}}

                    adapter.process(mock_payment, "test-token")

                    # Verify the mapped document type was used
                    call_args = adapter.sdk.payment().create.call_args[0][0]
                    assert call_args["payer"]["identification"]["type"] == "cpf_mapped"


def test_check_status_maps_known_status(adapter):
    """PAYMENT_STATUS_MAPPING is actually wired into check_status."""
    adapter.sdk.payment().get.return_value = {
        "response": {"id": "mp-payment-456", "status": "authorized", "status_detail": "accredited"}
    }

    result = adapter.check_status("mp-payment-456")

    assert result.status == PaymentStatuses.APPROVED


@patch("vinta_billing.services.payment_adapters.mercadopago_payment_adapter.logger")
def test_check_status_maps_unknown_status_and_logs(mock_logger, adapter):
    """An unrecognized provider status maps to UNKNOWN instead of being written raw."""
    response = {
        "response": {"id": "mp-payment-456", "status": "some_new_mp_status", "status_detail": "??"}
    }
    adapter.sdk.payment().get.return_value = response

    result = adapter.check_status("mp-payment-456")

    assert result.status == PaymentStatuses.UNKNOWN
    # Logs the id + status only — never `json.dumps(response)`, which would leak
    # payer PII (email, name, document number, billing address).
    mock_logger.error.assert_called_once_with(
        "Unknown payment status: payment_external_id=%s original_status=%s",
        "mp-payment-456",
        "some_new_mp_status",
    )


def test_get_event_id_derives_key_from_signed_material(adapter):
    """`get_event_id` no longer trusts the payload's unsigned top-level `id` — the
    ledger key is built entirely from the verified manifest (`data.id` +
    `x-request-id` + `ts`), matching `verify_signature`'s own source of truth."""
    ts = str(int(time.time()))
    raw_body, headers = build_signed_request(data_id="mp-payment-456", request_id="req-123", ts=ts)

    event_id = adapter.get_event_id(raw_body, headers, payload={"id": "attacker-controlled"})

    assert event_id == f"mp-payment-456:req-123:{ts}"


def test_get_event_id_ignores_payload_id_entirely(adapter):
    """Two deliveries with the same signed manifest but different (attacker-varied)
    top-level payload ids must resolve to the *same* ledger key — otherwise a
    single captured valid signature lets an attacker force unbounded reprocessing
    by mutating only the unsigned `id` field on each replay."""
    raw_body, headers = build_signed_request(data_id="mp-payment-456")

    event_id_1 = adapter.get_event_id(raw_body, headers, payload={"id": "notif-1"})
    event_id_2 = adapter.get_event_id(raw_body, headers, payload={"id": "notif-2-different"})

    assert event_id_1 == event_id_2


def test_get_event_id_raises_when_signature_invalid(adapter):
    raw_body, headers = build_signed_request(data_id="mp-payment-456")
    tampered_body = raw_body.replace(b"mp-payment-456", b"mp-payment-999")

    with pytest.raises(ProviderWebhookEventIdMissingError):
        adapter.get_event_id(tampered_body, headers, payload={"data": {"id": "mp-payment-456"}})


def test_verify_signature_accepts_correctly_signed_body(adapter):
    raw_body, headers = build_signed_request(data_id="mp-payment-456")

    assert adapter.verify_signature(raw_body, headers) is True


def test_verify_signature_rejects_tampered_body(adapter):
    """The signature covers `data.id`; a tampered id must fail even though the
    tampered body still parses to a well-formed, similar-looking payload."""
    raw_body, headers = build_signed_request(data_id="mp-payment-456")
    tampered_body = raw_body.replace(b"mp-payment-456", b"mp-payment-999")

    assert adapter.verify_signature(tampered_body, headers) is False


def test_verify_signature_rejects_missing_signature_header(adapter):
    raw_body = json.dumps({"data": {"id": "mp-payment-456"}}).encode()

    assert adapter.verify_signature(raw_body, {}) is False


def test_verify_signature_rejects_when_secret_not_configured(adapter):
    adapter.webhook_secret = ""
    raw_body, headers = build_signed_request(data_id="mp-payment-456")

    assert adapter.verify_signature(raw_body, headers) is False


def test_verify_signature_is_case_insensitive_to_header_names(adapter):
    """MercadoPago's own client casing (`X-Signature`) must be accepted."""
    raw_body, headers = build_signed_request(data_id="mp-payment-456")
    upper_headers = {k.upper(): v for k, v in headers.items()}

    assert adapter.verify_signature(raw_body, upper_headers) is True


def test_verify_signature_rejects_stale_timestamp(adapter):
    """A `ts` outside the tolerance window must be rejected even though the HMAC
    itself is perfectly valid — otherwise a single captured `(x-signature,
    x-request-id)` pair verifies forever."""
    stale_ts = str(int(time.time()) - 3600)  # 1h old, tolerance default is 300s
    raw_body, headers = build_signed_request(data_id="mp-payment-456", ts=stale_ts)

    assert adapter.verify_signature(raw_body, headers) is False


def test_verify_signature_rejects_non_integer_timestamp(adapter):
    """A non-integer `ts` must be rejected, not crash the request."""
    raw_body, headers = build_signed_request(data_id="mp-payment-456", ts="not-a-number")

    assert adapter.verify_signature(raw_body, headers) is False


@pytest.mark.parametrize("mapped_status", PAYMENT_STATUS_MAPPING.values())
def test_payment_status_mapping_values_are_valid_payment_statuses(mapped_status):
    """Every `PAYMENT_STATUS_MAPPING` value must be writeable to the
    `choices`-constrained `Payment.status` column."""
    assert mapped_status in PaymentStatuses.values


@pytest.mark.parametrize("mapped_status", REFUND_STATUS_MAPPING.values())
def test_refund_status_mapping_values_are_valid_refund_statuses(mapped_status):
    """Every `REFUND_STATUS_MAPPING` value must be writeable to the
    `choices`-constrained `Refund.status` column."""
    assert mapped_status in RefundStatuses.values


# --- Refund idempotency key ---


@patch(
    "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.config.RequestOptions"
)
def test_refund_idempotency_header_is_a_string(mock_request_options, adapter, mock_refund):
    """Regression: the header used to be the set literal ``{refund.id}``, which the
    SDK serialized as ``"{refund-123}"``. It must be a plain ``str``."""
    mock_options = Mock()
    mock_request_options.return_value = mock_options
    adapter.sdk.refund().create.return_value = {"response": {"id": "r-1", "status": "approved"}}

    adapter.refund(mock_refund)

    header_value = mock_options.custom_headers["x-idempotency-key"]
    assert isinstance(header_value, str)
    assert header_value == "refund-123"


@patch(
    "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.config.RequestOptions"
)
def test_refund_forwards_the_callers_idempotency_key(mock_request_options, adapter, mock_refund):
    mock_options = Mock()
    mock_request_options.return_value = mock_options
    adapter.sdk.refund().create.return_value = {"response": {"id": "r-1", "status": "approved"}}
    mock_refund.idempotency_key = "refund-key-1"

    adapter.refund(mock_refund)

    assert mock_options.custom_headers == {"x-idempotency-key": "refund-key-1"}


# --- Pix ---


PIX_RESPONSE = {
    "id": 123456789,
    "status": "pending",
    "status_detail": "pending_waiting_transfer",
    "date_of_expiration": "2026-10-14T12:30:00.000-03:00",
    "point_of_interaction": {
        "transaction_data": {
            "qr_code": "00020126580014br.gov.bcb.pix-synthetic",
            "qr_code_base64": "iVBORw0KGgo-synthetic",
            "ticket_url": "https://www.mercadopago.com.br/payments/123456789/ticket",
        }
    },
}


@pytest.fixture
def mock_pix_payment(mock_payment):
    mock_payment.payment_method = "pix"
    return mock_payment


@patch("vinta_billing.services.payment_adapters.mercadopago_payment_adapter.reverse")
@override_settings(SITE_DOMAIN="example.com")
def test_process_charge_sends_pix_without_a_card_token(mock_reverse, adapter, mock_pix_payment):
    mock_reverse.return_value = "/payment/update/mercadopago/payment-123/"
    adapter.sdk.payment().create.return_value = {"response": PIX_RESPONSE}
    expires_at = datetime.datetime(2026, 10, 14, 15, 30, tzinfo=datetime.UTC)

    adapter.process_charge(mock_pix_payment, "", expires_at=expires_at)

    payment_data = adapter.sdk.payment().create.call_args.args[0]
    assert payment_data["payment_method_id"] == "pix"
    assert "token" not in payment_data
    assert "installments" not in payment_data
    assert payment_data["payer"]["email"] == "test@example.com"
    assert payment_data["date_of_expiration"] == "2026-10-14T15:30:00.000+00:00"


@patch("vinta_billing.services.payment_adapters.mercadopago_payment_adapter.reverse")
@override_settings(SITE_DOMAIN="example.com")
def test_process_charge_returns_the_pix_qr_code_and_expiry(mock_reverse, adapter, mock_pix_payment):
    mock_reverse.return_value = "/payment/update/mercadopago/payment-123/"
    adapter.sdk.payment().create.return_value = {"response": PIX_RESPONSE}

    result = adapter.process_charge(mock_pix_payment, "")

    assert result.external_id == "123456789"
    assert result.status == PaymentStatuses.PENDING
    assert result.original_status == "pending"
    assert result.provider_data == PIX_RESPONSE["point_of_interaction"]["transaction_data"]
    assert result.expires_at == datetime.datetime(2026, 10, 14, 15, 30, tzinfo=datetime.UTC)


@patch("vinta_billing.services.payment_adapters.mercadopago_payment_adapter.reverse")
@override_settings(SITE_DOMAIN="example.com")
def test_process_charge_keeps_sending_the_card_token(mock_reverse, adapter, mock_payment):
    mock_reverse.return_value = "/payment/update/mercadopago/payment-123/"
    adapter.sdk.payment().create.return_value = {
        "response": {"id": 1, "status": "approved", "status_detail": "accredited"}
    }

    result = adapter.process_charge(mock_payment, "card-token")

    payment_data = adapter.sdk.payment().create.call_args.args[0]
    assert payment_data["token"] == "card-token"
    assert payment_data["installments"] == 1
    assert "date_of_expiration" not in payment_data
    assert result.status == PaymentStatuses.APPROVED
    assert result.provider_data is None
    assert result.expires_at is None


@override_settings(SITE_DOMAIN="example.com")
def test_process_charge_refuses_a_naive_expiry(adapter, mock_pix_payment):
    with pytest.raises(ValueError, match="timezone-aware"):
        adapter.process_charge(
            mock_pix_payment, "", expires_at=datetime.datetime(2026, 10, 14, 12, 30)
        )


def test_check_status_maps_an_expired_pix_charge_to_expired(adapter):
    """MercadoPago reports an unpaid Pix charge that ran out as ``cancelled`` with
    ``status_detail="expired"``."""
    adapter.sdk.payment().get.return_value = {
        "response": {"id": "1", "status": "cancelled", "status_detail": "expired"}
    }

    result = adapter.check_status("1")

    assert result.status == PaymentStatuses.EXPIRED
    assert result.original_status == "cancelled"


def test_check_status_keeps_a_called_off_charge_cancelled(adapter):
    adapter.sdk.payment().get.return_value = {
        "response": {"id": "1", "status": "cancelled", "status_detail": "by_collector"}
    }

    assert adapter.check_status("1").status == PaymentStatuses.CANCELLED


# --- Payments API request shape and error answers (0.9.1) ---


@pytest.fixture
def charge_adapter(adapter):
    """`adapter` with `reverse` stubbed and a public notification host."""
    with (
        patch(
            "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.reverse",
            return_value="/payment/update/mercadopago/payment-123/",
        ),
        override_settings(SITE_DOMAIN="example.com"),
    ):
        yield adapter


def test_process_charge_sends_a_numeric_amount_and_no_country(charge_adapter, mock_payment):
    mock_payment.value = Decimal("150.00")
    charge_adapter.sdk.payment().create.return_value = {
        "status": 201,
        "response": {"id": 1, "status": "approved", "status_detail": "accredited"},
    }

    charge_adapter.process_charge(mock_payment, "card-token")

    payment_data = charge_adapter.sdk.payment().create.call_args.args[0]
    assert payment_data["transaction_amount"] == 150.0
    assert isinstance(payment_data["transaction_amount"], float)
    assert "country" not in payment_data["payer"]["address"]
    assert json.loads(json.dumps(payment_data))["transaction_amount"] == 150


def test_process_charge_keeps_cents_exact(charge_adapter, mock_payment):
    mock_payment.value = Decimal("100.255")
    charge_adapter.sdk.payment().create.return_value = {"status": 201, "response": {"id": 1}}

    charge_adapter.process_charge(mock_payment, "card-token")

    assert charge_adapter.sdk.payment().create.call_args.args[0]["transaction_amount"] == 100.26


def test_process_charge_leaves_out_empty_address_fields(
    charge_adapter, mock_payment, mock_billing_address
):
    mock_billing_address.neighborhood = ""
    mock_billing_address.street_number = None
    charge_adapter.sdk.payment().create.return_value = {"status": 201, "response": {"id": 1}}

    charge_adapter.process_charge(mock_payment, "card-token")

    address = charge_adapter.sdk.payment().create.call_args.args[0]["payer"]["address"]
    assert address == {
        "zip_code": "12345-678",
        "street_name": "Test Street",
        "city": "Test City",
        "federal_unit": "Test State",
    }


def test_process_charge_raises_on_a_400_with_the_providers_message(charge_adapter, mock_payment):
    charge_adapter.sdk.payment().create.return_value = {
        "status": 400,
        "response": {
            "message": "notificaction_url attribute must be url valid",
            "error": "bad_request",
            "status": 400,
            "cause": [
                {"code": 4020, "description": "notificaction_url attribute must be url valid"}
            ],
        },
    }

    with pytest.raises(ProviderRequestRejectedError) as excinfo:
        charge_adapter.process_charge(mock_payment, "card-token")

    error = excinfo.value
    assert error.http_status == 400
    assert error.provider_message == "notificaction_url attribute must be url valid"
    assert error.causes == (("4020", "notificaction_url attribute must be url valid"),)
    assert "4020" in str(error)
    assert "test@example.com" not in str(error)


def test_process_charge_raises_on_a_success_without_an_id(charge_adapter, mock_payment):
    charge_adapter.sdk.payment().create.return_value = {"status": 200, "response": {}}

    with pytest.raises(ProviderRequestRejectedError):
        charge_adapter.process_charge(mock_payment, "card-token")


def test_process_charge_returns_a_declined_card_as_rejected(charge_adapter, mock_payment):
    """A charge MercadoPago created and declined is not an error."""
    charge_adapter.sdk.payment().create.return_value = {
        "status": 201,
        "response": {"id": 42, "status": "rejected", "status_detail": "cc_rejected_other_reason"},
    }

    result = charge_adapter.process_charge(mock_payment, "card-token")

    assert result.external_id == "42"
    assert result.status == PaymentStatuses.REJECTED
    assert result.original_status == "rejected"


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:3000", "127.0.0.1:8000", "api.mambu.localhost"]
)
def test_process_charge_omits_notification_url_for_a_local_host(adapter, mock_payment, host):
    adapter.sdk.payment().create.return_value = {"status": 201, "response": {"id": 1}}

    with override_settings(SITE_DOMAIN=host):
        adapter.process_charge(mock_payment, "card-token")

    assert "notification_url" not in adapter.sdk.payment().create.call_args.args[0]


@patch("vinta_billing.services.payment_adapters.mercadopago_payment_adapter.reverse")
@override_settings(SITE_DOMAIN="localhost:3000")
def test_notification_url_host_overrides_site_domain(mock_reverse, mock_payment):
    mock_reverse.return_value = "/payment/update/mercadopago/payment-123/"
    with patch(
        "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoPaymentAdapter.from_config(
            {"ACCESS_TOKEN": "token", "NOTIFICATION_URL_HOST": "abc.ngrok.app"}
        )
    adapter.sdk = mock_sdk.return_value
    adapter.sdk.payment().create.return_value = {"status": 201, "response": {"id": 1}}

    adapter.process_charge(mock_payment, "card-token")

    assert (
        adapter.sdk.payment().create.call_args.args[0]["notification_url"]
        == "https://abc.ngrok.app/payment/update/mercadopago/payment-123/"
    )


def test_refund_raises_on_an_error_answer(adapter, mock_refund):
    adapter.sdk.refund().create.return_value = {
        "status": 400,
        "response": {"message": "Invalid refund amount", "cause": [{"code": 2085}]},
    }

    with pytest.raises(ProviderRequestRejectedError, match="Invalid refund amount"):
        adapter.refund(mock_refund)


def test_check_status_raises_on_an_error_answer(adapter):
    adapter.sdk.payment().get.return_value = {
        "status": 404,
        "response": {"message": "Payment not found", "error": "not_found"},
    }

    with pytest.raises(ProviderRequestRejectedError) as excinfo:
        adapter.check_status("missing")

    assert excinfo.value.http_status == 404


def test_check_refund_status_raises_on_an_error_answer(adapter, mock_refund):
    mock_refund.external_id = "refund-456"
    adapter.sdk.refund().list_all.return_value = {
        "status": 401,
        "response": {"message": "invalid access token"},
    }

    with pytest.raises(ProviderRequestRejectedError):
        adapter.check_refund_status(mock_refund)
