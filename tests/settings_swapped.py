"""The test project again, with the organization models swapped out.

Everything ``billing`` stores about an organization is a foreign key, and every
one of them has to resolve through ``ORGANIZATION_MODEL`` rather than naming
``organizations.Organization``. That is invisible under the default settings --
the swappable model and the concrete model are the same class there -- so it
gets its own settings module and its own tox environment.

Run it with ``DJANGO_SETTINGS_MODULE=tests.settings_swapped``; ``tox -e
py311-django52-swapped`` does exactly that.
"""

from tests.settings import *  # noqa: F403
from tests.settings import INSTALLED_APPS


INSTALLED_APPS = [
    *INSTALLED_APPS,
    "tests.swapped_orgs.apps.SwappedOrgsConfig",
]

ORGANIZATION_MODEL = "swapped_orgs.Tenant"
ORGANIZATION_MEMBERSHIP_MODEL = "swapped_orgs.TenantMembership"
