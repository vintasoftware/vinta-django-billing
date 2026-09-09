"""The models the engine actually counts, each hanging off a billing scope.

Separate from 0001 so the payer table exists before the scope app builds its
foreign key to it. See that migration for the cycle this avoids.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("testapp", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        migrations.swappable_dependency(settings.BILLING_SCOPE_MODEL),
        # `swappable_dependency` resolves to ("vinta_billing", "__first__"),
        # which is 0001 -- three migrations before `BillingScope` exists. Any
        # project pointing a foreign key at the *shipped* scope model needs this
        # second, explicit dependency, or the graph is free to create these
        # tables first and fail with "Related model cannot be resolved". A
        # project that swapped the model needs only the line above.
        ("vinta_billing", "0003_billingscope"),
    ]

    operations = [
        migrations.CreateModel(
            name="Seat",
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
                ("is_active", models.BooleanField(default=True)),
                (
                    "scope",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.BILLING_SCOPE_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="testapp_seats",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="SeatInvitation",
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
                ("email", models.EmailField(max_length=254)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                (
                    "scope",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.BILLING_SCOPE_MODEL,
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="Widget",
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
                (
                    "scope",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.BILLING_SCOPE_MODEL,
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
    ]
