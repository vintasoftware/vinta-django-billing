"""Settings for the test project.

Deliberately minimal: the point is to prove the library works against a stock
``vinta-django-orgs`` installation, so nothing here configures a hierarchy, a
notifier or a manager predicate. The tests that need those override them.
"""

DEBUG = True
USE_TZ = True
SECRET_KEY = "test-only-key-not-used-anywhere-else"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

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
    "vinta_orgs.middleware.OrganizationMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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
}

# The metered resource `tests.testapp` registers. Named explicitly rather than
# left to inference so the setting itself is exercised.
VINTA_BILLING = {
    "METERED_RESOURCE_KEY": "event_occurrences",
}

USE_I18N = True
LANGUAGE_CODE = "en-us"
