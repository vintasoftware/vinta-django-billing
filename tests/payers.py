"""What each settings module bills, and how to make one.

``tests/conftest.py`` needs a payer to hang a scope off, and what a payer *is*
differs per settings module: a ``vinta_orgs`` organization by default, a
``testapp.Company`` wherever the scope model is one of the swapped-in ones. The
factory is named by ``TEST_PAYER_FACTORY`` so the fixtures never have to know
which module is in force.

Every factory takes a name and returns something the configured scope model's
``get_or_create_for`` accepts.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils.module_loading import import_string
from django.utils.text import slugify


def make_organization(name: str) -> Any:
    """A ``vinta-django-orgs`` organization -- the default settings' payer."""
    from vinta_orgs.conf import get_organization_model

    return get_organization_model().objects.create(name=name, slug=slugify(name))


def make_company(name: str) -> Any:
    """A ``testapp.Company`` -- the payer wherever the scope model is swapped."""
    from tests.testapp.models import Company

    return Company.objects.create(name=name)


def make_payer(name: str) -> Any:
    """Build whatever this settings module bills."""
    factory = import_string(
        getattr(settings, "TEST_PAYER_FACTORY", "tests.payers.make_organization")
    )
    return factory(name)


def payers_are_organizations() -> bool:
    """Whether the payer is a ``vinta-django-orgs`` organization.

    Read by the handful of tests that are *about* that integration --
    ``vinta_billing.contrib.orgs``, the membership predicates, the DRF mixin
    composition -- and which have nothing to say under a settings module whose
    payers are something else.
    """
    return (
        getattr(settings, "TEST_PAYER_FACTORY", "tests.payers.make_organization")
        == "tests.payers.make_organization"
    )


def scopes_can_name_users() -> bool:
    """Whether the configured scope model can bill a user directly.

    True for the shipped ``BillingScope`` (a generic key names anything) and for
    ``swapped_scopes.MixedScope``. False for ``swapped_scopes.CompanyScope``,
    which is deliberately a company-only model -- a project that bills exactly
    one kind of thing is a legitimate configuration, and the personal-plan tests
    have nothing to say about it.
    """
    return getattr(settings, "TEST_SCOPE_SUPPORTS_USERS", True)
