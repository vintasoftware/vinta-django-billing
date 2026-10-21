"""A one-time card charge through ``PaymentService.create_payment`` and the real
MercadoPago adapter, with only the SDK call mocked.

What a downstream checkout sees: an approved and a declined card both come back
as a ``Payment`` with the mapped status, and a request MercadoPago refuses comes
back as a ``PaymentError`` -- never a ``KeyError``.
"""

from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from vinta_billing.constants import PaymentProviders, PaymentStatuses
from vinta_billing.exceptions import PaymentError, ProviderRequestRejectedError
from vinta_billing.services.payment_adapters.mercadopago_payment_adapter import (
    MercadoPagoPaymentAdapter,
)
from vinta_billing.services.payment_service import PaymentService


MP = PaymentProviders.MERCADOPAGO


@pytest.fixture
def adapter():
    with patch(
        "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoPaymentAdapter("TEST-access-token")
    adapter.sdk = mock_sdk.return_value
    return adapter


@pytest.fixture
def service(adapter):
    resolver = Mock()
    resolver.resolve_for_scope.return_value = MP
    return PaymentService(
        payment_provider_resolver=resolver,
        payment_provider_registry={MP: adapter},
        subscription_provider_registry={},
    )


def charge(service, scope):
    return service.create_payment(
        scope, "BRL", Decimal("150.00"), "Course", "visa", "card-token", "idem-1"
    )


@pytest.mark.django_db
class TestMercadoPagoCardCheckout:
    @pytest.fixture(autouse=True)
    def site_domain(self, settings):
        settings.SITE_DOMAIN = "example.com"

    def test_an_approved_card(self, service, adapter, scope, billing_profile):
        adapter.sdk.payment().create.return_value = {
            "status": 201,
            "response": {"id": 1001, "status": "approved", "status_detail": "accredited"},
        }

        payment = charge(service, scope)

        payment.refresh_from_db()
        assert payment.external_id == "1001"
        assert payment.status == PaymentStatuses.APPROVED
        assert adapter.sdk.payment().create.call_args.args[0]["transaction_amount"] == 150.0

    def test_a_declined_card(self, service, adapter, scope, billing_profile):
        adapter.sdk.payment().create.return_value = {
            "status": 201,
            "response": {
                "id": 1002,
                "status": "rejected",
                "status_detail": "cc_rejected_other_reason",
            },
        }

        payment = charge(service, scope)

        payment.refresh_from_db()
        assert payment.status == PaymentStatuses.REJECTED
        assert payment.original_status == "rejected"

    @pytest.mark.parametrize(
        "answer",
        [
            {
                "status": 400,
                "response": {
                    "message": "The name of the following parameters is wrong : "
                    "[payer.address.country]",
                    "error": "bad_request",
                    "cause": [
                        {
                            "code": 8,
                            "description": "The name of the following parameters "
                            "is wrong : [payer.address.country]",
                        }
                    ],
                },
            },
            {
                "status": 401,
                "response": {"message": "Unauthorized use of live credentials", "code": 7},
            },
        ],
    )
    def test_a_refused_request_is_a_payment_error(
        self, service, adapter, scope, billing_profile, answer
    ):
        adapter.sdk.payment().create.return_value = answer

        with pytest.raises(PaymentError) as excinfo:
            charge(service, scope)

        assert isinstance(excinfo.value, ProviderRequestRejectedError)
        assert excinfo.value.http_status == answer["status"]
