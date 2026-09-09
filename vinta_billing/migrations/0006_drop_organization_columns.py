"""Drop the organization columns and tighten the scope ones. The destructive half.

Everything here assumes 0005 ran and the scope columns are filled in.

``BillingProfile`` is the awkward one. Its primary key *was* its payer
(``organization`` was ``primary_key=True``) and ``Payment.billing_profile_id``
holds those values, so the swap to a surrogate ``id`` has to leave the integers
exactly where they are or every payment detaches from its profile.

It is therefore done as three ``AlterField``/``RenameField`` steps rather than a
``RemoveField``/``AddField`` pair, and that is load-bearing rather than stylistic.
A remove-then-add tells Django two unrelated fields changed, so on SQLite -- where
a foreign key names its target column inside the *referencing* table's DDL -- the
``payment`` table is left pointing at a column that no longer exists and every
subsequent query dies with "foreign key mismatch". Altering the column in place
tells Django the primary key moved, which makes it rebuild the referencing tables
too. It also means no data is rewritten anywhere: the values ride through the
type change untouched, and no payment ever has to be re-pointed.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vinta_billing", "0005_backfill_scopes"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="subscription",
            options={"permissions": [("manage_billing", "Can manage the scope's billing")]},
        ),
        # Both constraints and the index name an organization column, so they
        # come off before it does and go back on against `scope` below.
        migrations.RemoveConstraint(
            model_name="meteredoccurrence",
            name="uniq_metered_occurrence",
        ),
        migrations.RemoveConstraint(
            model_name="paymentmethod",
            name="uniq_payment_method",
        ),
        migrations.RemoveIndex(
            model_name="billingperiodsummary",
            name="billing_period_org_idx",
        ),
        migrations.RemoveField(
            model_name="billingperiodsummary",
            name="organization",
        ),
        migrations.RemoveField(
            model_name="meteredoccurrence",
            name="organization",
        ),
        migrations.RemoveField(
            model_name="paymentmethod",
            name="organization",
        ),
        migrations.RemoveField(
            model_name="subscription",
            name="organization",
        ),
        migrations.RemoveField(
            model_name="billingperiodresourceusage",
            name="by_organization",
        ),
        # Step 1: stop being a foreign key, stay the primary key. Drops the
        # constraint into the organization table and renames the column
        # `organization_id` -> `organization`, carrying its values.
        migrations.AlterField(
            model_name="billingprofile",
            name="organization",
            field=models.BigIntegerField(primary_key=True, serialize=False),
        ),
        # Step 2: `organization` -> `id`. Still the primary key, still the same
        # integers, and Django re-points `payment` at the new column name.
        migrations.RenameField(
            model_name="billingprofile",
            old_name="organization",
            new_name="id",
        ),
        # Step 3: make it the auto field a surrogate key should be, so new
        # profiles get their key from the sequence rather than from a payer.
        migrations.AlterField(
            model_name="billingprofile",
            name="id",
            field=models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        # 0005 filled these, so they can carry their real NOT NULL now.
        migrations.AlterField(
            model_name="billingprofile",
            name="scope",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="billing_profile",
                to=settings.BILLING_SCOPE_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="subscription",
            name="scope",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="subscription",
                to=settings.BILLING_SCOPE_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="paymentmethod",
            name="scope",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="payment_methods",
                to=settings.BILLING_SCOPE_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="meteredoccurrence",
            name="scope",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="metered_occurrences",
                to=settings.BILLING_SCOPE_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="billingperiodsummary",
            name="scope",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="billing_period_summaries",
                to=settings.BILLING_SCOPE_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="billingperiodsummary",
            index=models.Index(
                fields=["scope", "-billing_period_start"], name="billing_period_org_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="meteredoccurrence",
            constraint=models.UniqueConstraint(
                fields=("scope", "event_id", "occurrence_start"),
                name="uniq_metered_occurrence",
            ),
        ),
        migrations.AddConstraint(
            model_name="paymentmethod",
            constraint=models.UniqueConstraint(
                fields=("scope", "provider", "external_id"), name="uniq_payment_method"
            ),
        ),
    ]
