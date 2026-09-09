"""The test project selling to users and to companies at once.

``swapped_scopes.MixedScope`` carries a nullable ``user`` and a nullable
``company`` with a CHECK constraint saying a scope is exactly one of the two.
This is the configuration the whole 0.8 change exists to make possible, and
running the suite against it is the difference between claiming a project can
bill both and showing it.

Run it with ``DJANGO_SETTINGS_MODULE=tests.settings_mixed``; ``tox -e mixed``
does exactly that.
"""

from tests.settings import *  # noqa: F403
from tests.settings import INSTALLED_APPS


INSTALLED_APPS = [
    *INSTALLED_APPS,
    "tests.swapped_scopes.apps.SwappedScopesConfig",
]

BILLING_SCOPE_MODEL = "swapped_scopes.MixedScope"
TEST_PAYER_FACTORY = "tests.payers.make_company"
