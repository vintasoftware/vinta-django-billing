"""Every organization relation in ``billing`` resolves through ``ORGANIZATION_MODEL``.

These only run under ``tests.settings_swapped``, where the organization model is
``swapped_orgs.Tenant`` and the concrete ``vinta_orgs.Organization`` has no
table at all. Under the default settings the two are the same class, so a
hardcoded ``"vinta_orgs.Organization"`` target would pass every other test in
the suite and only fail in a project that actually swapped the model.

The suite skips itself rather than failing when run under the default settings,
so ``pytest`` with no arguments stays meaningful.
"""

import pytest
from django.apps import apps
from django.conf import settings
from django.db import connection
from model_bakery import baker
from vinta_orgs.conf import get_organization_model

from billing.models import (
    BillingProfile,
    MeteredOccurrence,
    Subscription,
)


pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        getattr(settings, "ORGANIZATION_MODEL", "vinta_orgs.Organization") != "swapped_orgs.Tenant",
        reason="Only meaningful under tests.settings_swapped.",
    ),
]


#: Every ``billing`` model carrying a relation to an organization, and the field
#: name. Kept as an explicit list rather than discovered, so adding a model with
#: a new organization foreign key and forgetting to point it at the swappable
#: reference fails here instead of silently going untested.
ORGANIZATION_RELATIONS = [
    ("Subscription", "organization"),
    ("BillingProfile", "organization"),
    ("PaymentMethod", "organization"),
    ("MeteredOccurrence", "organization"),
    ("BillingPeriodSummary", "organization"),
]


def test_the_stock_organization_model_really_is_swapped_out():
    """Guards the premise of every other test here.

    If ``vinta_orgs.Organization`` still had a table, a hardcoded foreign key
    to it would keep working and the rest of this module would prove nothing.
    """
    stock = apps.get_model("vinta_orgs", "Organization")

    assert stock._meta.swapped == "swapped_orgs.Tenant"
    assert stock._meta.db_table not in connection.introspection.table_names()


@pytest.mark.parametrize(("model_name", "field_name"), ORGANIZATION_RELATIONS)
def test_organization_relations_point_at_the_swapped_model(model_name, field_name):
    model = apps.get_model("billing", model_name)
    field = model._meta.get_field(field_name)

    assert field.related_model is get_organization_model()


def test_the_swapped_model_is_not_the_stock_one():
    """A sanity check on the check: ``related_model is get_organization_model()``
    would also hold if nothing had been swapped."""
    assert get_organization_model() is not apps.get_model("vinta_orgs", "Organization")
    assert hasattr(get_organization_model(), "external_reference")


def test_a_subscription_round_trips_against_the_swapped_model():
    """Not just resolvable at the schema level -- actually writable.

    ``fields.E301`` catches a foreign key pointing at a swapped-out model, but a
    system check passing is not the same as the column existing and the insert
    landing.
    """
    tenant = baker.make(get_organization_model(), external_reference="acme-1")
    plan = baker.make("billing.BillingPlan", slug="swap-test", is_active=True)
    subscription = baker.make(Subscription, organization=tenant, plan=plan)

    reloaded = Subscription.objects.get(pk=subscription.pk)

    assert reloaded.organization == tenant
    assert reloaded.organization.external_reference == "acme-1"


def test_the_system_checks_pass_under_the_swap():
    """``fields.E301`` is raised for a relation to a swapped-out model.

    Running the checks here is what turns "the foreign keys look right" into
    "Django agrees they are right", including for any relation this module's
    explicit list has not been updated for.
    """
    from django.core.checks import Error, run_checks

    errors = [
        message
        for message in run_checks()
        if isinstance(message, Error) and message.id in {"fields.E300", "fields.E301"}
    ]

    assert errors == []


def test_billing_profile_and_metered_occurrence_accept_the_swapped_model():
    """The two relations most likely to be missed: one on the write path from a
    view, one written by the meter from a background job."""
    tenant = baker.make(get_organization_model())
    billing_address = baker.make("billing.BillingAddress")
    profile = baker.make(
        BillingProfile,
        organization=tenant,
        contact_email="billing@example.com",
        document_type="CPF",
        document_number="12345678900",
        billing_address=billing_address,
    )
    plan = baker.make("billing.BillingPlan", slug="swap-test-2", is_active=True)
    subscription = baker.make(Subscription, organization=tenant, plan=plan)
    occurrence = baker.make(
        MeteredOccurrence,
        organization=tenant,
        subscription=subscription,
    )

    assert BillingProfile.objects.get(pk=profile.pk).organization == tenant
    assert MeteredOccurrence.objects.get(pk=occurrence.pk).organization == tenant
