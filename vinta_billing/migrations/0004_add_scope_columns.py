"""Add the scope columns beside the organization ones. Nothing is dropped here.

Additive by design: this migration is safe to apply to a running installation,
0005 fills the new columns in, and only 0006 removes anything. Splitting it that
way is what makes the upgrade recoverable -- an operator who stops after 0004 or
0005 still has every organization column and every row intact.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vinta_billing", "0003_billingscope"),
        # Named here rather than left implicit: once a project swaps the scope
        # model out, the foreign keys below point into *that* project's app, and
        # without this the graph is free to run this migration first and fail
        # with "Related model cannot be resolved". Harmless when nothing is
        # swapped -- Django ignores a dependency on the migration's own app.
        migrations.swappable_dependency(settings.BILLING_SCOPE_MODEL),
    ]

    operations = [
        # `is_default_for_new_organizations` is a plain boolean whose meaning did
        # not change, so it is a true rename rather than a drop and re-add: the
        # column keeps its data and no plan silently loses its default flag. The
        # constraint has to come off first because it names the column.
        migrations.RemoveConstraint(
            model_name="billingplan",
            name="uniq_default_billing_plan",
        ),
        migrations.RenameField(
            model_name="billingplan",
            old_name="is_default_for_new_organizations",
            new_name="is_default_for_new_scopes",
        ),
        migrations.AddConstraint(
            model_name="billingplan",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_default_for_new_scopes", True)),
                fields=("is_default_for_new_scopes",),
                name="uniq_default_billing_plan",
            ),
        ),
        # The organization columns go nullable *here*, three migrations before
        # they are dropped, and that ordering is what makes the whole upgrade
        # reversible. Rolling back runs 0006 first (re-adding each column as the
        # state before the drop declared it -- nullable, and empty), then 0005
        # (which refills them), then 0004 (which restores NOT NULL against
        # populated columns). Making them nullable in 0006 instead would restore
        # NOT NULL before 0005 had put any values back, and the rollback would
        # fail on the first table with rows in it.
        migrations.AlterField(
            model_name="billingperiodsummary",
            name="organization",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="billing_period_summaries",
                to=settings.ORGANIZATION_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="meteredoccurrence",
            name="organization",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="metered_occurrences",
                to=settings.ORGANIZATION_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="paymentmethod",
            name="organization",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="payment_methods",
                to=settings.ORGANIZATION_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="subscription",
            name="organization",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="subscription",
                to=settings.ORGANIZATION_MODEL,
            ),
        ),
        # Nullable for now. 0006 tightens them once 0005 has filled them in.
        migrations.AddField(
            model_name="billingprofile",
            name="scope",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="billing_profile",
                to=settings.BILLING_SCOPE_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="subscription",
            name="scope",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="subscription",
                to=settings.BILLING_SCOPE_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="scope",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="payment_methods",
                to=settings.BILLING_SCOPE_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="meteredoccurrence",
            name="scope",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="metered_occurrences",
                to=settings.BILLING_SCOPE_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="billingperiodsummary",
            name="scope",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="billing_period_summaries",
                to=settings.BILLING_SCOPE_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="billingperiodresourceusage",
            name="by_scope",
            field=models.JSONField(default=dict),
        )
    ]
