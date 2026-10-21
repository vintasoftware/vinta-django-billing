"""Run the MercadoPago release checks against the sandbox through the shipped adapter.

One approved card (cardholder ``APRO``), one refused card (``OTHE``), one Pix
charge and a full refund of the approved charge, on site ``MLB``, with
MercadoPago's published test cards. Prints statuses and ids only, never a key or
a card token.

Test credentials only. ``TEST-`` keys go with the Payments API; ``APP_USR-``
test keys from a Checkout Transparente app go with the Orders API. Never run
this with production credentials: it creates real charges against them.

    MERCADOPAGO_ACCESS_TOKEN=... MERCADOPAGO_PUBLIC_KEY=... \\
    MERCADOPAGO_PAYER_EMAIL=buyer@example.com \\
    uv run python scripts/mercadopago_sandbox_check.py

With ``TEST-`` keys use an ordinary email as payer (a test user's email gets
"Invalid users involved").
"""

import datetime
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import django
import requests


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
django.setup()

from django.conf import settings  # noqa: E402

from vinta_billing.exceptions import PaymentError  # noqa: E402
from vinta_billing.services.payment_adapters.mercadopago_payment_adapter import (  # noqa: E402
    MercadoPagoPaymentAdapter,
)


VISA = "4235647728025682"
CPF = "12345678909"


def card_token(public_key: str, cardholder: str) -> str:
    response = requests.post(
        "https://api.mercadopago.com/v1/card_tokens",
        params={"public_key": public_key},
        json={
            "card_number": VISA,
            "expiration_month": 11,
            "expiration_year": 2030,
            "security_code": "123",
            "cardholder": {"name": cardholder, "identification": {"type": "CPF", "number": CPF}},
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["id"]


def payment(payer_email: str, payment_method: str) -> SimpleNamespace:
    address = SimpleNamespace(
        street_name="Av. das Nações Unidas",
        street_number="3003",
        neighborhood="Bonfim",
        city="Osasco",
        state="SP",
        country="BR",
        zip_code="06233-200",
    )
    return SimpleNamespace(
        id=str(uuid.uuid4()),
        value=Decimal("150.00"),
        description="vinta-django-billing sandbox check",
        payment_method=payment_method,
        external_id="",
        billing_profile=SimpleNamespace(
            email=payer_email,
            first_name="APRO",
            last_name="Sandbox",
            document_type="CPF",
            document_number=CPF,
            billing_address=address,
        ),
    )


def main() -> int:
    access_token = os.environ["MERCADOPAGO_ACCESS_TOKEN"]
    public_key = os.environ["MERCADOPAGO_PUBLIC_KEY"]
    payer_email = os.environ["MERCADOPAGO_PAYER_EMAIL"]
    # A local host keeps notification_url out of the request, so nothing needs
    # to be routable for these checks.
    settings.SITE_DOMAIN = "localhost"
    settings.VINTA_BILLING = {**getattr(settings, "VINTA_BILLING", {}), "SITE_DOMAIN": "localhost"}

    adapter = MercadoPagoPaymentAdapter.from_config({"ACCESS_TOKEN": access_token})
    print(f"adapter: {type(adapter).__name__}")
    failures = 0

    def check(label, expected, run):
        nonlocal failures
        try:
            outcome = run()
        except PaymentError as error:
            outcome = f"{type(error).__name__}: {error}"
        ok = expected(outcome)
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {label}: {outcome}")
        return outcome

    approved = check(
        "card APRO",
        lambda r: getattr(r, "status", None) == "approved",
        lambda: adapter.process_charge(
            payment(payer_email, "visa"), card_token(public_key, "APRO")
        ),
    )
    check(
        "card OTHE",
        lambda r: getattr(r, "status", None) == "rejected",
        lambda: adapter.process_charge(
            payment(payer_email, "visa"), card_token(public_key, "OTHE")
        ),
    )
    check(
        "pix",
        lambda r: "qr_code" in (getattr(r, "provider_data", None) or {}),
        lambda: adapter.process_charge(
            payment(payer_email, "pix"),
            "",
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=30),
        ),
    )
    if getattr(approved, "external_id", None):
        charged = payment(payer_email, "visa")
        charged.external_id = approved.external_id
        refund = SimpleNamespace(
            id=str(uuid.uuid4()),
            payment=charged,
            value=charged.value,
            idempotency_key="",
            external_id=None,
        )
        check(
            "full refund",
            lambda r: getattr(r, "status", None) in {"approved", "pending"},
            lambda: adapter.refund(refund),
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
