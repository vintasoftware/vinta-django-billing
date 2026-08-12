from django.apps import AppConfig


class TestAppConfig(AppConfig):
    name = "tests.testapp"
    label = "testapp"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # Registration has to happen after the app registry is populated (the
        # counters import models) and before anything serves a request or
        # renders a resource dropdown. `ready()` is the only hook that is both.
        from tests.testapp.billing_resources import register

        register()
