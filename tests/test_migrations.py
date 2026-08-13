"""Properties of the shipped migrations that are easy to break silently."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import models

from vinta_billing.constants import LimitKind
from vinta_billing.models import PlanLimit
from vinta_billing.registry import ResourceRegistry, resource_choices, resources


@pytest.mark.django_db
def test_no_model_changes_are_unmigrated():
    """`makemigrations --check` must be clean on a fresh checkout.

    Needs database access: the autodetector consults the migration loader,
    which reads `django_migrations`.
    """
    out = StringIO()

    call_command("makemigrations", "--check", "--dry-run", stdout=out, stderr=out)


def test_resource_fields_take_choices_by_reference():
    """The migration must reference the callable, not a snapshot of its result.

    This is what makes the resource registry *open*: a project registering a new
    resource changes what `resource_choices()` returns, and if the migration had
    frozen the list, every registration would demand a schema migration for a
    column whose type never changed.
    """
    field = PlanLimit._meta.get_field("resource_key")
    _name, _path, _args, kwargs = field.deconstruct()

    assert kwargs["choices"] is resource_choices


def test_registering_a_resource_does_not_ask_for_a_migration():
    """The end-to-end version of the property above."""
    field = PlanLimit._meta.get_field("resource_key")
    before = field.deconstruct()

    registry_backup = dict(resources._definitions)
    try:
        resources.register(
            "a_brand_new_resource",
            label="Brand new",
            kind=LimitKind.PREPAID,
            counter=lambda context: {},
        )
        assert field.deconstruct() == before
    finally:
        resources._definitions.clear()
        resources._definitions.update(registry_backup)


@pytest.mark.django_db
def test_the_organization_column_points_at_the_swappable_model():
    """Scoped tables must follow `ORGANIZATION_MODEL`, not a hardcoded table.

    Getting this wrong only shows up in a project that swapped the model, which
    is exactly the project least able to work around it.
    """
    from vinta_orgs.conf import get_organization_model

    from vinta_billing.models import BillingProfile

    field = BillingProfile._meta.get_field("organization")

    assert isinstance(field, models.ForeignKey | models.OneToOneField)
    assert field.related_model is get_organization_model()


def test_a_fresh_registry_starts_empty():
    """Nothing is registered by the package itself.

    If this ever fails, the engine has grown an opinion about what it bills for.
    """
    assert len(ResourceRegistry()) == 0
