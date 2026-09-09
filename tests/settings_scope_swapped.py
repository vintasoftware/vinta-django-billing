"""The test project with ``BILLING_SCOPE_MODEL`` pointed at a project's own model.

``swapped_scopes.CompanyScope`` is an ``AbstractBillingScope`` subclass with a
real foreign key where the shipped model has a generic key. Under the default
settings the swappable reference and the concrete model are the same class, so a
relation hardcoded to ``vinta_billing.BillingScope`` passes the whole suite and
only breaks in a project that swapped the model -- which is exactly the project
least able to work around it. This settings module is what makes that failure
visible here.

The payer changes with it: a ``CompanyScope`` names a ``testapp.Company``, not a
``vinta-django-orgs`` organization, so ``TEST_PAYER_FACTORY`` moves too and the
handful of tests that are specifically about the organizations integration skip
themselves.

Run it with ``DJANGO_SETTINGS_MODULE=tests.settings_scope_swapped``; ``tox -e
scopeswapped`` does exactly that.
"""

from tests.settings import *  # noqa: F403
from tests.settings import INSTALLED_APPS


INSTALLED_APPS = [
    *INSTALLED_APPS,
    "tests.swapped_scopes.apps.SwappedScopesConfig",
]

BILLING_SCOPE_MODEL = "swapped_scopes.CompanyScope"
TEST_PAYER_FACTORY = "tests.payers.make_company"

#: ``CompanyScope`` names a company and nothing else, on purpose: a project that
#: bills exactly one kind of payer is a legitimate configuration, and running the
#: suite against one is part of what this module is for.
TEST_SCOPE_SUPPORTS_USERS = False
