"""Only ``Company``, and deliberately only ``Company``.

The scoped models live in 0002 because of a cycle that is otherwise unavoidable:
``swapped_scopes.CompanyScope`` points at ``Company``, and ``Widget``, ``Seat``
and ``SeatInvitation`` point at the scope model. With all four in one migration
the graph has ``testapp`` depending on ``swapped_scopes`` depending on
``testapp``, and Django refuses it.

Splitting the payer out breaks the cycle: this migration depends on nothing, the
scope app depends on it, and 0002 depends on the scope app.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Company",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=100)),
                ("is_own_root", models.BooleanField(default=False)),
                (
                    "parent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="children",
                        to="testapp.company",
                    ),
                ),
            ],
        ),
    ]
