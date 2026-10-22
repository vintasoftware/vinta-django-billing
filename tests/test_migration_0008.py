"""``0008`` is additive and reverses cleanly.

Rewinds to ``0007`` and replays forward, on a database holding a status update
written before the migration, and checks it comes back as a webhook's. Ends on
the latest migration again, so the rest of the suite runs against the schema it
expects.
"""

from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader

from vinta_billing.constants import PaymentStatusUpdateSource


pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = ("vinta_billing", "0007_payment_provider_data_refund_idempotency_key")


def _latest():
    (leaf,) = MigrationLoader(None, ignore_no_migrations=True).graph.leaf_nodes("vinta_billing")
    return leaf


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
    from vinta_billing.models import Payment, PaymentStatusUpdate

    payment = Payment.objects.create(
        billing_profile=billing_profile,
        currency="BRL",
        value=Decimal("10.00"),
        payment_method="pix",
        status="approved",
        payment_provider="mercadopago",
        external_id="pre-0008",
    )
    PaymentStatusUpdate.objects.create(payment=payment, status="approved")

    try:
        old_apps = _migrate(BEFORE)
        assert "source" not in _columns("vinta_billing_paymentstatusupdate")
        assert old_apps.get_model("vinta_billing", "PaymentStatusUpdate").objects.count() == 1
    finally:
        _migrate(_latest())

    assert PaymentStatusUpdate.objects.get(payment=payment).source == (
        PaymentStatusUpdateSource.WEBHOOK
    )
