"""MercadoPago one-time payments through the Orders API (``/v1/orders``).

Selected with ``VINTA_BILLING["PROVIDERS"]["mercadopago"]["API"] = "orders"``
(see ``MercadoPagoPaymentAdapter.from_config``). Apps created for Checkout
Transparente on newer accounts get ``APP_USR-`` test credentials, which only the
Orders API accepts: ``/v1/payments`` answers them with ``401 "Unauthorized use of
live credentials"``.

Subclasses ``MercadoPagoPaymentAdapter`` for what both APIs share -- the
credentials, the payment-method and document-type mappings, the payer address,
and the webhook signature, which is the same ``x-signature`` HMAC over ``data.id``
-- and overrides every call that reaches the API.

Unlike the Payments API, amounts are decimal strings (``"150.00"``), the result
is an order (``ORD...``) holding one payment transaction (``PAY...``), and a
refused card answers ``402`` instead of a created payment with
``status: rejected``.

Reference: https://www.mercadopago.com.br/developers/pt/reference/orders/online-payments
"""

import datetime
import logging
from decimal import Decimal
from typing import Any

import mercadopago.config
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from vinta_billing.constants import PaymentStatuses, RefundStatuses
from vinta_billing.services.dataclasses import ChargeResult, Refund, RefundResult
from vinta_billing.services.mercadopago_api import CENTS, response_or_raise
from vinta_billing.services.payment_adapters.base import Payment, PaymentStatusUpdate
from vinta_billing.services.payment_adapters.mercadopago_payment_adapter import (
    DOCUMENT_TYPES_MAPPING,
    EXPIRED_STATUS_DETAIL,
    PAYMENT_METHODS_MAPPING,
    PIX_TRANSACTION_DATA_KEYS,
    MercadoPagoPaymentAdapter,
    payer_address,
)


logger = logging.getLogger(__name__)

#: ``payment_method.type`` for each MercadoPago ``payment_method.id``. A method
#: missing here is sent as ``credit_card``, the only type the card brands use.
PAYMENT_METHOD_TYPES: dict[str, str] = {
    "visa": "credit_card",
    "master": "credit_card",
    "amex": "credit_card",
    "diners": "credit_card",
    "elo": "credit_card",
    "hipercard": "credit_card",
    "debvisa": "debit_card",
    "debmaster": "debit_card",
    "debelo": "debit_card",
    "pix": "bank_transfer",
    "bolbradesco": "ticket",
}
DEFAULT_PAYMENT_METHOD_TYPE = "credit_card"
#: Payment-method types charged without a card token.
TOKENLESS_PAYMENT_METHOD_TYPES: frozenset[str] = frozenset({"bank_transfer", "ticket"})
#: The longest ``expiration_time`` the Orders API accepts.
MAX_EXPIRATION = datetime.timedelta(hours=2920)

# Order statuses: https://www.mercadopago.com.br/developers/pt/docs/checkout-api-orders/payment-management/status/order-status
ORDER_STATUS_MAPPING: dict[str, str] = {
    "created": PaymentStatuses.PENDING,
    # `waiting_payment` / `waiting_transfer` (Pix, boleto), `waiting_capture`,
    # `waiting_retry`: someone still has to act before money moves.
    "action_required": PaymentStatuses.PENDING,
    "processing": PaymentStatuses.IN_PROCESS,
    "processed": PaymentStatuses.APPROVED,
    "failed": PaymentStatuses.REJECTED,
    "canceled": PaymentStatuses.CANCELLED,
    "refunded": PaymentStatuses.REFUNDED,
    "charged_back": PaymentStatuses.CHARGED_BACK,
}
#: ``(status, status_detail)`` pairs that mean something the status alone does
#: not. ``expired`` comes under both ``canceled`` and ``failed``: either way it is
#: a Pix (or boleto) code nobody paid in time.
ORDER_STATUS_DETAIL_MAPPING: dict[tuple[str, str], str] = {
    ("processed", "partially_refunded"): PaymentStatuses.PARTIALLY_REFUNDED,
    ("canceled", EXPIRED_STATUS_DETAIL): PaymentStatuses.EXPIRED,
    ("failed", EXPIRED_STATUS_DETAIL): PaymentStatuses.EXPIRED,
    ("charged_back", "in_process"): PaymentStatuses.IN_MEDIATION,
}
#: ``transactions.refunds[].status``.
ORDER_REFUND_STATUS_MAPPING: dict[str, str] = {
    "processed": RefundStatuses.APPROVED,
    "processing": RefundStatuses.PENDING,
    "in_process": RefundStatuses.PENDING,
    "pending": RefundStatuses.PENDING,
    "failed": RefundStatuses.REJECTED,
    "rejected": RefundStatuses.REJECTED,
}
#: The Orders API's notification ``type``. Its actions (``order.processed``,
#: ``order.refunded``, ``order.canceled``, ``order.failed``, ``order.expired``,
#: ``order.action_required``...) all mean "the order changed": ``check_status``
#: reads the current state off the API rather than off the unsigned payload.
ORDER_NOTIFICATION_TYPE = "order"
ORDER_NOTIFICATION_ACTION_PREFIX = "order."
#: The ``provider_data`` key holding the order's payment transaction id
#: (``PAY...``), which a partial refund names.
TRANSACTION_ID_KEY = "transaction_id"


def map_order_status(status: str | None, status_detail: str | None = None) -> str:
    """An order's ``status`` / ``status_detail`` as a ``PaymentStatuses`` value;
    ``UNKNOWN`` when unmapped."""
    if not status:
        return PaymentStatuses.UNKNOWN
    detailed = ORDER_STATUS_DETAIL_MAPPING.get((status, status_detail or ""))
    if detailed:
        return detailed
    return ORDER_STATUS_MAPPING.get(status, PaymentStatuses.UNKNOWN)


def string_amount(value: Decimal) -> str:
    """``value`` as the two-decimal string the Orders API requires."""
    return str(Decimal(value).quantize(CENTS))


def iso8601_duration(delta: datetime.timedelta) -> str:
    """``delta`` as an ISO 8601 duration (``PT1H30M``), whole seconds."""
    seconds = int(delta.total_seconds())
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    duration = "PT"
    if hours:
        duration += f"{hours}H"
    if minutes:
        duration += f"{minutes}M"
    if seconds or duration == "PT":
        duration += f"{seconds}S"
    return duration


def _first_payment(order: dict) -> dict:
    payments = (order.get("transactions") or {}).get("payments") or []
    return payments[0] if payments else {}


class MercadoPagoOrdersPaymentAdapter(MercadoPagoPaymentAdapter):
    @property
    def is_configured(self) -> bool:
        """See ``BasePaymentAdapter.is_configured``. The same ``ACCESS_TOKEN``
        as the Payments API adapter -- an ``APP_USR-`` one, for the Orders API."""
        return bool(self.access_token)

    def process_charge(
        self,
        payment: Payment,
        payment_token: str,
        idempotency_key: str = "",
        *,
        expires_at: datetime.datetime | None = None,
    ) -> ChargeResult:
        """Create and process an order. See ``BasePaymentAdapter.process_charge``.

        ``expires_at`` becomes the order's ``expiration_time`` (an ISO 8601
        duration from now). A Pix charge's QR code and ticket URL come back
        under ``transactions.payments[0].payment_method`` and are returned as
        ``provider_data``, together with the payment transaction's id.

        A refused card answers ``402``; that is a declined charge, not an error,
        and comes back with status ``REJECTED``.
        """
        if expires_at is not None and timezone.is_naive(expires_at):
            raise ValueError("expires_at must be timezone-aware.")
        payment_method_id = PAYMENT_METHODS_MAPPING.get(
            payment.payment_method, payment.payment_method
        )
        payment_method_type = PAYMENT_METHOD_TYPES.get(
            payment_method_id, DEFAULT_PAYMENT_METHOD_TYPE
        )
        payment_method: dict[str, Any] = {"id": payment_method_id, "type": payment_method_type}
        if payment_method_type not in TOKENLESS_PAYMENT_METHOD_TYPES:
            payment_method["token"] = payment_token
            payment_method["installments"] = 1

        profile = payment.billing_profile
        payer: dict[str, Any] = {
            "email": profile.email,
            "first_name": profile.first_name,
            "last_name": profile.last_name,
            "identification": {
                "type": DOCUMENT_TYPES_MAPPING.get(
                    profile.document_type or "", profile.document_type
                ),
                "number": profile.document_number,
            },
        }
        address = payer_address(profile.billing_address)
        if "federal_unit" in address:
            # The Orders API names it `state`.
            address["state"] = address.pop("federal_unit")
        if address:
            payer["address"] = address

        amount = string_amount(payment.value)
        order_data: dict[str, Any] = {
            "type": "online",
            "processing_mode": "automatic",
            "total_amount": amount,
            "external_reference": str(payment.id),
            "description": payment.description,
            "payer": payer,
            "transactions": {"payments": [{"amount": amount, "payment_method": payment_method}]},
        }
        if expires_at is not None:
            order_data["expiration_time"] = self._expiration_time(expires_at)

        result = self.sdk.order().create(
            order_data, self._request_options(idempotency_key or str(payment.id))
        )
        if result.get("status") == 402:
            return self._declined_charge_result(result)
        return self._order_charge_result(response_or_raise(result), expires_at)

    @staticmethod
    def _request_options(idempotency_key: str):
        """``RequestOptions`` carrying ``idempotency_key``, which every Orders
        API write requires. The SDK sends a random lowercase
        ``x-idempotency-key`` of its own, so this overrides that exact key."""
        request_options = mercadopago.config.RequestOptions()
        request_options.custom_headers = {"x-idempotency-key": idempotency_key}
        return request_options

    @staticmethod
    def _expiration_time(expires_at: datetime.datetime) -> str:
        remaining = expires_at - timezone.now()
        if remaining <= datetime.timedelta(0):
            raise ValueError("expires_at must be in the future.")
        return iso8601_duration(min(remaining, MAX_EXPIRATION))

    def _order_charge_result(
        self, order: dict, requested_expires_at: datetime.datetime | None
    ) -> ChargeResult:
        transaction = _first_payment(order)
        payment_method = transaction.get("payment_method") or {}
        provider_data = {
            key: payment_method[key] for key in PIX_TRANSACTION_DATA_KEYS if payment_method.get(key)
        }
        if transaction.get("id"):
            provider_data[TRANSACTION_ID_KEY] = transaction["id"]
        expires_at = requested_expires_at
        if transaction.get("date_of_expiration"):
            expires_at = parse_datetime(transaction["date_of_expiration"]) or expires_at
        original_status = order.get("status")
        return ChargeResult(
            external_id=str(order["id"]),
            status=map_order_status(original_status, order.get("status_detail")),
            original_status=original_status,
            provider_data=provider_data or None,
            expires_at=expires_at,
        )

    def _declined_charge_result(self, result: dict) -> ChargeResult:
        """A ``402``: the card was refused. The body carries ``errors`` and,
        depending on the case, the order -- whose id becomes the external id.
        Without one there is nothing to look up later, which is still a
        declined charge rather than an adapter error."""
        body = result.get("response") or {}
        order = body.get("data") if isinstance(body.get("data"), dict) else body
        order_id = order.get("id") if isinstance(order, dict) else None
        logger.info(
            "MercadoPago declined an order: order_id=%s errors=%s",
            order_id,
            [error.get("code") for error in body.get("errors") or () if isinstance(error, dict)],
        )
        transaction = _first_payment(order) if isinstance(order, dict) else {}
        return ChargeResult(
            external_id=str(order_id) if order_id else "",
            status=PaymentStatuses.REJECTED,
            original_status=(order or {}).get("status") or "failed",
            provider_data=(
                {TRANSACTION_ID_KEY: transaction["id"]} if transaction.get("id") else None
            ),
        )

    def check_status(
        self, payment_external_id: str, update_id: str | None = None
    ) -> PaymentStatusUpdate:
        order = response_or_raise(self.sdk.order().get(str(payment_external_id)))
        original_status = order.get("status")
        mapped_status = map_order_status(original_status, order.get("status_detail"))
        if mapped_status == PaymentStatuses.UNKNOWN:
            logger.error(
                "Unknown order status: payment_external_id=%s original_status=%s",
                payment_external_id,
                original_status,
            )
        return PaymentStatusUpdate(
            id=None,
            status=mapped_status,
            description=order.get("status_detail"),
            # From the authenticated response, never the unsigned notification;
            # see `MercadoPagoPaymentAdapter.check_status`.
            update_external_id=order.get("id"),
            original_status=original_status,
        )

    def receive_update(self, update_payload: dict) -> tuple[str, PaymentStatusUpdate] | None:
        if update_payload.get("type") != ORDER_NOTIFICATION_TYPE or not str(
            update_payload.get("action") or ""
        ).startswith(ORDER_NOTIFICATION_ACTION_PREFIX):
            return None
        # `BasePaymentAdapter.receive_update`, skipping the Payments API filter.
        return super(MercadoPagoPaymentAdapter, self).receive_update(update_payload)

    def refund(self, refund: Refund) -> RefundResult:
        """Refund the order: in full without a body, or in part naming its
        payment transaction and the amount.

        Reads the order first, for the transaction id and for whether this
        refund covers everything that is left.
        """
        order_id = str(refund.payment.external_id)
        order = response_or_raise(self.sdk.order().get(order_id))
        transaction = _first_payment(order)
        refunded = sum(
            (
                Decimal(item.get("amount") or "0")
                for item in (order.get("transactions") or {}).get("refunds") or []
            ),
            Decimal("0"),
        )
        full = not refunded and Decimal(refund.value) >= Decimal(
            transaction.get("amount") or order.get("total_amount") or "0"
        )
        body = (
            None
            if full
            else {
                "transactions": [
                    {"id": transaction.get("id"), "amount": string_amount(refund.value)}
                ]
            }
        )
        result = self.sdk.order().refund_transaction(
            order_id, body, self._request_options(refund.idempotency_key or str(refund.id))
        )
        response = response_or_raise(result)
        refunds = (response.get("transactions") or {}).get("refunds") or []
        # The answer lists every refund of the order; this one is the newest.
        latest = refunds[-1] if refunds else {}
        original_status = latest.get("status") or ""
        mapped_status = ORDER_REFUND_STATUS_MAPPING.get(original_status, RefundStatuses.UNKNOWN)
        if mapped_status == RefundStatuses.UNKNOWN:
            logger.error(
                "Unknown refund status: refund_id=%s original_status=%s",
                refund.id,
                original_status,
            )
        return RefundResult(external_id=str(latest.get("id") or ""), status=mapped_status)

    def check_refund_status(self, refund: Refund) -> str:
        """Reads the refund off ``GET /v1/orders/{id}``: the Orders API has no
        refund lookup of its own."""
        if not refund.external_id:
            logger.error(
                "Cannot check refund status without an external_id: refund_id=%s", refund.id
            )
            return RefundStatuses.UNKNOWN
        order = response_or_raise(self.sdk.order().get(str(refund.payment.external_id)))
        for item in (order.get("transactions") or {}).get("refunds") or []:
            if str(item.get("id")) == str(refund.external_id):
                original_status = item.get("status") or ""
                mapped_status = ORDER_REFUND_STATUS_MAPPING.get(
                    original_status, RefundStatuses.UNKNOWN
                )
                if mapped_status == RefundStatuses.UNKNOWN:
                    logger.error(
                        "Unknown refund status: refund_external_id=%s original_status=%s",
                        refund.external_id,
                        original_status,
                    )
                return mapped_status
        logger.error(
            "Refund not found on its order: refund_external_id=%s payment_external_id=%s",
            refund.external_id,
            refund.payment.external_id,
        )
        return RefundStatuses.UNKNOWN
