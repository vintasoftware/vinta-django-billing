"""The smallest settings module that installs this package, and nothing else.

Deliberately not built on ``tests.settings``: that module installs
``vinta_orgs`` and ``tests.testapp``, and the point here is a project that has
neither. No ``ORGANIZATION_MODEL``, no organization app, no billing resources
registered -- just the package, on an empty database.

Used by ``tests/no_orgs_smoke.py`` under ``tox -e noorgs``.
"""

SECRET_KEY = "not-a-secret-this-is-a-test-settings-module"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "vinta_billing.apps.BillingConfig",
]

USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
