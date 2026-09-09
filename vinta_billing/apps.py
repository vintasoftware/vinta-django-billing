from django.apps import AppConfig

from vinta_billing import conf


# Before any model in this app is imported -- see the function's docstring for
# why that matters, and why this is not in ``ready()``.
conf.install_swappable_defaults()


class BillingConfig(AppConfig):
    name = "vinta_billing"
    verbose_name = "Billing"
    default_auto_field = "django.db.models.BigAutoField"
