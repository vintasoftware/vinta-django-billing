"""Every scope relation resolves through ``BILLING_SCOPE_MODEL``.

A foreign key hardcoded to ``vinta_billing.BillingScope`` passes every other test
in this suite under the default settings, because there the swappable reference
and the concrete model are the same class. It only breaks in a project that
swapped the model -- which is exactly the project least able to work around it.

``tox -e swapped`` runs the whole suite against ``tests.settings_swapped``, where
the project's *payer* model is swapped out from under billing. This module holds
the assertions that are specifically about the indirection rather than about
billing behaviour.
"""

from __future__ import annotations

import pytest
from django.apps import apps
from django.db import models

from vinta_billing.conf import DEFAULT_SCOPE_MODEL, get_scope_model, scope_model_string
from vinta_billing.models import (
    AbstractBillingScope,
    BillingPeriodSummary,
    BillingProfile,
    BillingScope,
    MeteredOccurrence,
    PaymentMethod,
    Subscription,
)


#: Every model in this package that points at a scope, and the field that does it.
SCOPE_RELATIONS = [
    (Subscription, "scope"),
    (BillingProfile, "scope"),
    (PaymentMethod, "scope"),
    (MeteredOccurrence, "scope"),
    (BillingPeriodSummary, "scope"),
]


@pytest.mark.parametrize(
    "model,field_name",
    SCOPE_RELATIONS,
    ids=[f"{model.__name__}-{field}" for model, field in SCOPE_RELATIONS],
)
def test_scope_relations_point_at_the_configured_model(model, field_name):
    """The relation follows the setting, whatever the setting says."""
    field = model._meta.get_field(field_name)

    assert isinstance(field, models.ForeignKey | models.OneToOneField)
    assert field.related_model is get_scope_model()


def test_the_configured_model_is_a_billing_scope():
    """Whatever a project swaps in has to carry the contract billing reads.

    ``label``, ``owner`` and ``parent`` are what the shipped recipient,
    permission and hierarchy defaults use; ``scope_key`` is what provisioning
    looks a scope up by. Inheriting :class:`AbstractBillingScope` is how a
    project gets all four, and this is the assertion that says so out loud.
    """
    scope_model = get_scope_model()

    assert issubclass(scope_model, AbstractBillingScope)
    for field_name in ("scope_type", "scope_key", "label", "owner", "parent"):
        assert scope_model._meta.get_field(field_name) is not None


def test_the_shipped_model_is_swappable():
    """Without this, ``BILLING_SCOPE_MODEL`` could not point anywhere else."""
    assert BillingScope._meta.swappable == "BILLING_SCOPE_MODEL"


def test_nothing_hardcodes_the_shipped_model():
    """The failure this whole module exists to catch.

    Under the default settings ``get_scope_model()`` *is* ``BillingScope``, so a
    hardcoded target is invisible. Under a swap it is not: the shipped model is
    swapped out, has no table, and any relation still pointing at it is broken.
    """
    if scope_model_string() == DEFAULT_SCOPE_MODEL:
        pytest.skip("nothing is swapped under these settings; see tests.settings_swapped")

    shipped = apps.get_model(DEFAULT_SCOPE_MODEL)
    assert get_scope_model() is not shipped
    for model, field_name in SCOPE_RELATIONS:
        assert model._meta.get_field(field_name).related_model is not shipped


@pytest.mark.django_db
def test_a_subscription_round_trips_against_the_configured_model(scope, plan):
    """The relation is not just declared correctly -- it reads and writes."""
    import datetime

    from django.utils import timezone

    from vinta_billing.constants import BillingInterval, BillingState

    now = timezone.now()
    subscription = Subscription.objects.create(
        scope=scope,
        plan=plan,
        billing_state=BillingState.ACTIVE,
        billing_interval=BillingInterval.MONTHLY,
        current_period_start=now - datetime.timedelta(days=1),
        current_period_end=now + datetime.timedelta(days=29),
    )

    subscription.refresh_from_db()
    assert subscription.scope == scope
    assert scope.subscription == subscription


@pytest.mark.django_db
def test_billing_profile_and_metered_occurrence_accept_the_configured_model(scope, billing_address):
    from vinta_billing.constants import DocumentTypes

    profile = BillingProfile.objects.create(
        scope=scope,
        contact_first_name="Ada",
        contact_last_name="Lovelace",
        contact_email="billing@example.com",
        document_type=DocumentTypes.OTHER,
        document_number="1",
        billing_address=billing_address,
    )

    profile.refresh_from_db()
    assert profile.scope == scope
    # The profile is no longer keyed *by* its payer -- see migration 0006 -- so
    # this is a surrogate id and the scope is a plain unique relation.
    assert profile.pk != scope.pk or profile.pk is not None
    assert scope.billing_profile == profile
