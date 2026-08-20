"""Settings for the test project.

Deliberately minimal: the point is to prove the library works against a stock
``vinta-django-orgs`` installation, so nothing here configures a hierarchy, a
notifier or a manager predicate. The tests that need those override them.
"""

import os


DEBUG = True
USE_TZ = True
SECRET_KEY = "test-only-key-not-used-anywhere-else"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# SQLite by default -- fast, needs nothing installed, and enough for
# everything the suite asserts about rows and queries.
#
# Except one thing. SQLite has no row locks: Django notices
# `has_select_for_update` is False and drops the clause rather than raising, so
# `CycleCloseService.close_subscription`'s `SELECT ... FOR UPDATE` is silently a
# plain SELECT there, and a concurrency test written against it would pass
# whether or not the lock was ever taken. `tests/test_cycle_close_concurrency.py`
# is skipped unless the database actually supports the lock, and this is the
# switch that gives it one: set `VINTA_BILLING_TEST_POSTGRES_HOST` (see the
# `postgres` environment in `tox.ini`, which CI runs).
_postgres_host = os.environ.get("VINTA_BILLING_TEST_POSTGRES_HOST")
if _postgres_host:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": _postgres_host,
            "PORT": os.environ.get("VINTA_BILLING_TEST_POSTGRES_PORT", "5432"),
            "NAME": os.environ.get("VINTA_BILLING_TEST_POSTGRES_DB", "postgres"),
            "USER": os.environ.get("VINTA_BILLING_TEST_POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("VINTA_BILLING_TEST_POSTGRES_PASSWORD", "postgres"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

ROOT_URLCONF = "tests.urls"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sites",
    "django.contrib.sessions",
    "django.contrib.messages",
    "rest_framework",
    "vinta_orgs.apps.OrganizationsConfig",
    "vinta_billing.apps.BillingConfig",
    # Registers the resources and entitlements the suite bills for, standing in
    # for a host application. Nothing in `billing` knows these exist.
    "tests.testapp.apps.TestAppConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # After `AuthenticationMiddleware`, which `vinta_orgs.W001` checks for: from
    # `vinta-django-orgs` 0.3 the middleware refuses an organization the caller
    # holds no active membership in, and it needs `request.user` to do that.
    # Placed earlier, the check silently does nothing and any authenticated
    # caller can select any tenant by naming its slug.
    "vinta_orgs.middleware.OrganizationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

SITE_ID = 1

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    # Set here rather than inside the one test that generates a schema.
    # `extend_schema` builds its inspector class from whatever
    # DEFAULT_SCHEMA_CLASS was when a view's `schema` was first touched, and
    # caches it -- so an `override_settings` that arrives after some earlier
    # test has resolved a route is silently too late, and the generator then
    # calls drf-spectacular's inspector through DRF's incompatible base. A
    # project mounting these routes sets this in its own settings anyway; the
    # shipped viewsets carry `@extend_schema` unconditionally.
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # Every throttle scope the shipped viewsets name. DRF's `ScopedRateThrottle`
    # raises `ImproperlyConfigured` for a scope with no rate, so a project that
    # mounts these routes without all three gets a 500 on the endpoint instead
    # of a throttle. The numbers are the test project's, not the package's --
    # a rate limit is a deployment's decision.
    "DEFAULT_THROTTLE_RATES": {
        "payment-webhook": "120/min",
        "payment-provider": "60/min",
        "billing-write": "30/min",
    },
}

# The metered resource `tests.testapp` registers. Named explicitly rather than
# left to inference so the setting itself is exercised.
VINTA_BILLING = {
    "METERED_RESOURCE_KEY": "event_occurrences",
}

USE_I18N = True
LANGUAGE_CODE = "en-us"
