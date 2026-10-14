"""``0007`` is additive and reverses cleanly.

Rewinds to ``0006`` and replays forward, on a database holding a payment and a
refund written before the migration, and checks the rows survive both ways.
Ends on ``0007`` again, so the rest of the suite runs against the schema it
expects.
"""

from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = ("vinta_billing", "0006_drop_organization_columns")
AFTER = ("vinta_billing", "0007_payment_provider_data_refund_idempotency_key")


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])
    executor.loader.build_graph()
    return executor.loader.project_state([target]).apps


def _columns(table):
    with connection.cursor() as cursor:
        return {c.name for c in connection.introspection.get_table_description(cursor, table)}


def test_unapplying_and_reapplying_keeps_existing_rows(billing_profile):
    from vinta_billing.models import Payment, Refund

    payment = Payment.objects.create(
        billing_profile=billing_profile,
        currency="BRL",
        value=Decimal("10.00"),
        payment_method="visa",
        status="approved",
        payment_provider="mercadopago",
        external_id="pre-0007",
    )
    Refund.objects.create(payment=payment, value=Decimal("1.00"), currency="BRL", status="approved")

    try:
        old_apps = _migrate(BEFORE)
        assert "provider_data" not in _columns("vinta_billing_payment")
        assert "expires_at" not in _columns("vinta_billing_payment")
        assert "idempotency_key" not in _columns("vinta_billing_refund")
        assert old_apps.get_model("vinta_billing", "Payment").objects.count() == 1
        assert old_apps.get_model("vinta_billing", "Refund").objects.count() == 1
    finally:
        _migrate(AFTER)

    payment.refresh_from_db()
    assert payment.provider_data is None
    assert payment.expires_at is None
    assert Refund.objects.get(payment=payment).idempotency_key == ""
