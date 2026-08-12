from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = "billing"
    verbose_name = "Billing"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # Registers the notification contexts the dunning ladder renders. A
        # no-op when vintasend is not installed -- the module guards its own
        # import, so a project using a different transport is not forced to
        # install one it does not use.
        from billing import notification_contexts  # noqa: F401
