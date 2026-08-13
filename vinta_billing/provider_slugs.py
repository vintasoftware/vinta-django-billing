"""Payment provider slugs, free of Django imports.

Separate from :mod:`vinta_billing.constants` because a project may need to validate a
configured provider at settings-import time, and ``vinta_billing.constants`` cannot be
imported that early: its ``TextChoices`` call ``gettext_lazy`` at class-body
evaluation, which touches ``django.conf.settings`` while settings are still
mid-import.

Keep this module pure stdlib -- no Django imports, and nothing that imports
Django -- so it stays safe to import from a settings module.
"""

STRIPE = "stripe"
MERCADOPAGO = "mercadopago"

#: The providers this package ships adapters for. A project can register more
#: through the provider registry; this tuple is only the built-in set.
PAYMENT_PROVIDER_SLUGS: tuple[str, ...] = (
    STRIPE,
    MERCADOPAGO,
)
