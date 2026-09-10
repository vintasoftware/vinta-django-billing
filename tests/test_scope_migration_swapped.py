"""The 0.7 -> 0.8 migrations, run against a swapped scope model.

``tests/test_scope_migration.py`` covers the upgrade itself, and skips whenever
``BILLING_SCOPE_MODEL`` points anywhere but the shipped model -- reasonably, since
the backfill's whole job there is to write generic-keyed rows. What that leaves
untested is the *other* project: one that swapped the model, has no upgrade to
perform, and still has to be able to run and unwind the migration chain.

That project could not. ``0003`` creates ``BillingScope`` with
``options={"swappable": "BILLING_SCOPE_MODEL"}``, so Django skips it and no
table exists; ``0005`` then reached for
``apps.get_model("vinta_billing", "BillingScope")`` regardless, and its manager
raises ``Manager isn't available; 'vinta_billing.BillingScope' has been swapped
for ...``. Forwards got away with it -- there are no rows on a fresh database, so
it returns before touching the model -- but ``backwards`` read the scope table
unconditionally, so **rolling a deploy back failed on a database with no billing
data at all**.

These tests run only under a swapped scope model, which means ``tox -e
scopeswapped`` and ``tox -e mixed``.
"""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from vinta_billing.conf import DEFAULT_SCOPE_MODEL, get_scope_model, scope_model_string


pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        scope_model_string() == DEFAULT_SCOPE_MODEL,
        reason="this module is about the swapped-in scope model; the shipped one is covered by tests/test_scope_migration.py",
    ),
]

BACKFILL = ("vinta_billing", "0005_backfill_scopes")
BEFORE_BACKFILL = ("vinta_billing", "0004_add_scope_columns")
HEAD = ("vinta_billing", "0006_drop_organization_columns")


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.migrate([target])
    executor.loader.build_graph()


def test_the_chain_unwinds_and_reapplies_under_a_swapped_scope_model():
    """The regression: reversing past the backfill, then going forward again.

    No data and no assertions about rows -- the failure this pins is that the
    migration could not *run* at all. It raised before reaching anything worth
    asserting about.
    """
    _migrate(BEFORE_BACKFILL)
    _migrate(HEAD)


def test_the_backfill_is_inert_when_there_is_nothing_to_migrate():
    """A project that swapped the model on a fresh database has no upgrade.

    Its scope table is its own and this migration must leave it alone, rather
    than inventing generic-keyed rows in a model that has no generic key.
    """
    _migrate(BEFORE_BACKFILL)
    _migrate(HEAD)

    assert get_scope_model()._default_manager.count() == 0


def test_the_configured_model_is_what_the_migration_resolves():
    """Guards the fix itself rather than its symptom.

    If this ever resolves back to the shipped model, the two tests above go on
    passing on an empty database and the bug returns for anyone with rows.
    """
    # `import_module` rather than an `import` statement: the module name starts
    # with a digit, so it is not a legal identifier.
    backfill = importlib.import_module("vinta_billing.migrations.0005_backfill_scopes")

    assert backfill._scope_model(apps)._meta.label == settings.BILLING_SCOPE_MODEL
    assert backfill._generic_key_scope_model(apps) is None
