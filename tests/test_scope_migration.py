"""The 0.7 -> 0.8 upgrade, run against rows that were written before scopes.

Everything else in the suite exercises the *end state*: a database built by
running every migration against no data at all. That says nothing about the one
thing an adopter actually depends on, which is that their existing billing rows
come out the other side attached to the right payer.

So these tests migrate back to 0003, write rows through the historical models
exactly as 0.7 left them, migrate forward, and check what happened. The two
claims worth the machinery:

* every organization becomes exactly one scope, and every billing row follows it;
* ``BillingProfile``'s primary key keeps its values, so the ``Payment`` rows
  hanging off it stay attached -- the failure mode this would otherwise have is
  silent, and it detaches payment history from payers.
"""

from __future__ import annotations

import datetime

import pytest
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings


# Rewinding and replaying migrations needs a real, non-transactional database:
# `migrate` manages its own transactions and the usual test wrapper would fight
# it.
#
# Skipped under `tests.settings_swapped`, and the reason is about the fixtures
# rather than about the migrations. The rows below are written through
# `vinta_orgs.Organization` because that is the model a 0.7 installation was
# billing -- under a swap it has no table to write to. The forward migrations
# themselves are exercised there regardless: creating the test database for that
# settings module runs all six.
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        getattr(settings, "ORGANIZATION_MODEL", "vinta_orgs.Organization")
        != "vinta_orgs.Organization",
        reason="the 0.7 upgrade fixtures are written against the stock organization model",
    ),
]

LEGACY = ("vinta_billing", "0003_billingscope")
CURRENT = ("vinta_billing", "0006_drop_organization_columns")

#: What an upgrading project sets so the backfill knows what its scopes name.
UPGRADE_SETTINGS = {"LEGACY_SCOPE_MODEL": "vinta_orgs.Organization"}


def _migrate(target):
    """Run the graph to ``target`` and hand back the resulting model registry."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])
    executor.loader.build_graph()
    return executor.loader.project_state([target]).apps


@pytest.fixture
def at_legacy():
    """Rewind to just before the scope columns exist, and restore afterwards.

    The restore runs under the upgrade settings because one test deliberately
    leaves the graph part-applied by refusing to guess, and teardown still has
    to get the database back to the current schema for the next test.
    """
    apps = _migrate(LEGACY)
    yield apps
    with override_settings(VINTA_BILLING=UPGRADE_SETTINGS):
        _migrate(CURRENT)


def _write_legacy_rows(apps):
    """Two organizations' worth of 0.7-shaped billing data."""
    Organization = apps.get_model("vinta_orgs", "Organization")
    BillingPlan = apps.get_model("vinta_billing", "BillingPlan")
    BillingProfile = apps.get_model("vinta_billing", "BillingProfile")
    BillingAddress = apps.get_model("vinta_billing", "BillingAddress")
    Payment = apps.get_model("vinta_billing", "Payment")
    Subscription = apps.get_model("vinta_billing", "Subscription")

    acme = Organization.objects.create(name="Acme", slug="acme")
    globex = Organization.objects.create(name="Globex", slug="globex")

    plan = BillingPlan.objects.create(
        name="Starter", slug="starter", monthly_price="10.00", annual_price="100.00"
    )

    now = datetime.datetime.now(tz=datetime.UTC)
    profiles = {}
    for organization in (acme, globex):
        address = BillingAddress.objects.create(
            street_name="Main",
            street_number="1",
            city="Springfield",
            state="SP",
            country="BR",
            zip_code="00000-000",
        )
        profiles[organization.slug] = BillingProfile.objects.create(
            organization=organization,
            contact_first_name="Ada",
            contact_last_name="Lovelace",
            contact_email="billing@example.com",
            document_type="OTHER",
            document_number="1",
            billing_address=address,
        )
        Subscription.objects.create(
            organization=organization,
            plan=plan,
            billing_state="active",
            billing_interval="monthly",
            current_period_start=now - datetime.timedelta(days=1),
            current_period_end=now + datetime.timedelta(days=29),
        )

    # The rows whose attachment the pk swap could silently break.
    for slug, profile in profiles.items():
        Payment.objects.create(
            billing_profile=profile,
            value="10.00",
            currency="USD",
            payment_provider="stripe",
            external_id="pay-%s" % slug,
            status="approved",
            original_status="approved",
            payment_method="card",
        )

    return {"acme": acme, "globex": globex, "profiles": profiles}


def test_every_organization_becomes_one_scope_and_the_rows_follow(at_legacy):
    legacy = _write_legacy_rows(at_legacy)

    with override_settings(VINTA_BILLING=UPGRADE_SETTINGS):
        apps = _migrate(CURRENT)

    BillingScope = apps.get_model("vinta_billing", "BillingScope")
    Subscription = apps.get_model("vinta_billing", "Subscription")

    assert BillingScope.objects.count() == 2
    for organization in (legacy["acme"], legacy["globex"]):
        scope = BillingScope.objects.get(object_id=str(organization.pk))
        assert scope.scope_type == "organization"
        assert scope.scope_key == "vinta_orgs.organization:%d" % organization.pk
        # The subscription that named this organization now names its scope.
        assert Subscription.objects.get(scope=scope) is not None

    # Two organizations, two scopes, and no row left behind.
    assert Subscription.objects.filter(scope__isnull=True).count() == 0


def test_payments_stay_attached_to_their_profile_across_the_pk_swap(at_legacy):
    """The claim the ``AlterField`` chain in 0006 exists to make good on.

    ``BillingProfile``'s primary key was the organization id and
    ``Payment.billing_profile_id`` holds those values, so a pk swap that
    renumbers detaches every payment. Nothing raises when that happens -- the
    rows just point at the wrong profile, or at none.
    """
    legacy = _write_legacy_rows(at_legacy)
    expected = {
        "pay-acme": legacy["profiles"]["acme"].pk,
        "pay-globex": legacy["profiles"]["globex"].pk,
    }

    with override_settings(VINTA_BILLING=UPGRADE_SETTINGS):
        apps = _migrate(CURRENT)

    Payment = apps.get_model("vinta_billing", "Payment")
    BillingProfile = apps.get_model("vinta_billing", "BillingProfile")

    for external_id, original_profile_pk in expected.items():
        payment = Payment.objects.get(external_id=external_id)
        assert payment.billing_profile_id == original_profile_pk
        # And the profile it names is still there, now carrying a scope.
        profile = BillingProfile.objects.get(pk=payment.billing_profile_id)
        assert profile.scope_id is not None


def test_the_backfill_is_idempotent(at_legacy):
    """Re-running after a partial failure must not double up the scopes."""
    _write_legacy_rows(at_legacy)
    backfill = ("vinta_billing", "0005_backfill_scopes")

    with override_settings(VINTA_BILLING=UPGRADE_SETTINGS):
        apps = _migrate(backfill)
        scopes_after_first = apps.get_model("vinta_billing", "BillingScope").objects.count()

        # Back to the additive step and forward again: `get_or_create` on
        # (content_type, object_id) has to resolve to the same two rows.
        _migrate(("vinta_billing", "0004_add_scope_columns"))
        apps = _migrate(backfill)

    assert scopes_after_first == 2
    assert apps.get_model("vinta_billing", "BillingScope").objects.count() == scopes_after_first


def test_the_backfill_refuses_to_guess_what_the_scopes_name(at_legacy):
    """With rows to migrate and no ``LEGACY_SCOPE_MODEL``, it stops.

    Guessing would produce scopes pointing at the wrong content type, which is
    both silent and very hard to unpick afterwards -- so it fails with a message
    naming the setting instead.
    """
    _write_legacy_rows(at_legacy)

    with override_settings(VINTA_BILLING={}), pytest.raises(Exception, match="LEGACY_SCOPE_MODEL"):
        _migrate(("vinta_billing", "0005_backfill_scopes"))


def test_a_fresh_install_needs_no_legacy_setting(at_legacy):
    """No rows, no setting, no complaint -- the path every new project takes."""
    with override_settings(VINTA_BILLING={}):
        apps = _migrate(CURRENT)

    assert apps.get_model("vinta_billing", "BillingScope").objects.count() == 0


def test_the_whole_upgrade_rolls_back(at_legacy):
    """0004-0006 undo cleanly, which is the point of splitting them.

    An operator who gets partway and needs to stop has to be able to. The
    organization columns are made nullable before they are dropped precisely so
    that re-adding them on the way back does not fail against existing rows.
    """
    legacy = _write_legacy_rows(at_legacy)

    with override_settings(VINTA_BILLING=UPGRADE_SETTINGS):
        _migrate(CURRENT)
        apps = _migrate(LEGACY)

    Subscription = apps.get_model("vinta_billing", "Subscription")
    BillingProfile = apps.get_model("vinta_billing", "BillingProfile")

    # Every row is back on its organization, with the values it started with.
    assert set(Subscription.objects.values_list("organization_id", flat=True)) == {
        legacy["acme"].pk,
        legacy["globex"].pk,
    }
    assert set(BillingProfile.objects.values_list("organization_id", flat=True)) == {
        legacy["acme"].pk,
        legacy["globex"].pk,
    }
