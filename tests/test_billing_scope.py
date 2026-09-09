"""The scope model itself, before anything points at it.

Two claims are worth their own tests here, because both are invariants the rest
of the package will lean on and neither is checked anywhere else: ``scope_key``
is always in step with the columns it is derived from, and one project can hold
a user scope and an organization scope side by side.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q

from vinta_billing.conf import DEFAULT_SCOPE_MODEL, scope_model_string
from vinta_billing.constants import ScopeType
from vinta_billing.models import BillingScope


# This module is about the *shipped* scope model specifically -- its generic
# key, its manager, its constraints. Under a settings module that swapped it
# out, `BillingScope` has no table and none of it applies; the contract every
# scope model has to honour is checked in `test_swappable_models.py` instead.
pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        scope_model_string() != DEFAULT_SCOPE_MODEL,
        reason="the shipped scope model is swapped out under these settings",
    ),
]


class TestGetOrCreateFor:
    def test_a_user_becomes_a_user_scope_owned_by_that_user(self, user):
        scope, created = BillingScope.objects.get_or_create_for(user)

        assert created is True
        assert scope.scope_type == ScopeType.USER
        # The one inference that matters for the shipped permission default:
        # a personal plan is manageable by the person, out of the box.
        assert scope.owner_id == user.pk
        assert scope.scope == user

    def test_an_organization_becomes_an_organization_scope_with_no_owner(self, organization):
        scope, created = BillingScope.objects.get_or_create_for(organization)

        assert created is True
        assert scope.scope_type == ScopeType.ORGANIZATION
        assert scope.label == "Acme"
        # Nothing here can know which member owns an organization's billing --
        # that is what BILLING_MANAGER_PREDICATE is for.
        assert scope.owner_id is None

    def test_both_kinds_coexist_in_one_project(self, user, organization):
        """The whole reason this model exists."""
        personal, _ = BillingScope.objects.get_or_create_for(user)
        team, _ = BillingScope.objects.get_or_create_for(organization)

        assert personal.pk != team.pk
        assert {personal.scope_type, team.scope_type} == {
            ScopeType.USER,
            ScopeType.ORGANIZATION,
        }
        assert personal.scope == user
        assert team.scope == organization

    def test_second_call_returns_the_same_row(self, organization):
        first, created_first = BillingScope.objects.get_or_create_for(organization)
        second, created_second = BillingScope.objects.get_or_create_for(organization)

        assert created_first is True
        assert created_second is False
        assert first.pk == second.pk
        assert BillingScope.objects.count() == 1

    def test_second_call_does_not_overwrite_a_curated_label(self, organization):
        scope, _ = BillingScope.objects.get_or_create_for(organization)
        scope.label = "Acme (billing contact: finance@acme.test)"
        scope.save()

        again, created = BillingScope.objects.get_or_create_for(organization)

        assert created is False
        assert again.label == "Acme (billing contact: finance@acme.test)"

    def test_two_payers_sharing_a_pk_across_models_do_not_collide(self, user, organization):
        """A user and an organization can both be pk 1. The content type separates them."""
        organization.pk = user.pk
        organization.save()

        personal, _ = BillingScope.objects.get_or_create_for(user)
        team, _ = BillingScope.objects.get_or_create_for(organization)

        assert personal.pk != team.pk
        assert personal.scope_key != team.scope_key

    def test_an_explicit_scope_type_wins_over_the_inference(self, organization):
        scope, _ = BillingScope.objects.get_or_create_for(organization, scope_type="workspace")

        assert scope.scope_type == "workspace"

    def test_a_payer_with_no_name_falls_back_to_str(self, db):
        payer = get_user_model().objects.create_user(username="grace", password="pw")

        scope, _ = BillingScope.objects.get_or_create_for(payer)

        assert scope.label == str(payer)


class TestScopeKey:
    def test_it_names_the_model_and_the_pk(self, organization):
        scope, _ = BillingScope.objects.get_or_create_for(organization)

        app_label = organization._meta.app_label
        model_name = organization._meta.model_name
        assert scope.scope_key == f"{app_label}.{model_name}:{organization.pk}"

    def test_it_is_rebuilt_on_every_save(self, user, organization):
        scope, _ = BillingScope.objects.get_or_create_for(organization)
        original = scope.scope_key

        scope.scope = user
        scope.save()

        assert scope.scope_key != original
        assert scope.scope_key.endswith(f":{user.pk}")

    def test_a_partial_update_that_moves_the_scope_still_writes_the_key(self, user, organization):
        """The guard that stops a scope silently detaching from its own billing rows.

        ``update_fields`` naming only the scope columns would leave ``scope_key``
        holding the previous payer, and every lookup by key would find the wrong
        scope -- or none.
        """
        scope, _ = BillingScope.objects.get_or_create_for(organization)

        scope.scope = user
        scope.save(update_fields=["content_type", "object_id", "modified"])

        scope.refresh_from_db()
        assert scope.scope_key.endswith(f":{user.pk}")

    def test_it_is_unique_per_scope_type(self, organization):
        BillingScope.objects.get_or_create_for(organization)

        with pytest.raises(IntegrityError), transaction.atomic():
            BillingScope.objects.create(
                scope_type=ScopeType.ORGANIZATION,
                content_type=BillingScope.objects.get().content_type,
                object_id=str(organization.pk),
            )


class TestValidateScope:
    def test_a_scope_naming_nothing_is_refused(self, organization):
        scope = BillingScope(
            content_type=BillingScope.objects.get_or_create_for(organization)[0].content_type,
            object_id="",
        )

        with pytest.raises(ValidationError, match="must name something"):
            scope.save()

    def test_the_database_refuses_it_too(self, organization):
        """``save`` is bypassed by ``bulk_create``; the CHECK constraint is not."""
        content_type = BillingScope.objects.get_or_create_for(organization)[0].content_type

        with pytest.raises(IntegrityError), transaction.atomic():
            BillingScope.objects.bulk_create(
                [BillingScope(content_type=content_type, object_id="")]
            )


class TestHierarchy:
    def test_a_scope_can_carry_a_parent(self, user, organization):
        root, _ = BillingScope.objects.get_or_create_for(organization)
        child, _ = BillingScope.objects.get_or_create_for(user, parent=root)

        assert child.parent_id == root.pk
        assert list(BillingScope.objects.filter(parent=root)) == [child]

    def test_deleting_a_parent_is_refused(self, user, organization):
        """A reseller cannot be deleted out from under the subtree it pays for."""
        root, _ = BillingScope.objects.get_or_create_for(organization)
        BillingScope.objects.get_or_create_for(user, parent=root)

        from django.db.models import ProtectedError

        with pytest.raises(ProtectedError), transaction.atomic():
            root.delete()


class TestSurvivability:
    def test_a_scope_outlives_the_payer_it_names(self, organization):
        """Deleting an organization must not take its payment history with it.

        Nothing constrains the generic key back to the payer's table, which is
        the point: the label and the billing rows stay readable afterwards.
        """
        scope, _ = BillingScope.objects.get_or_create_for(organization)
        organization.delete()

        scope.refresh_from_db()
        assert scope.label == "Acme"
        assert scope.scope is None


def test_the_shipped_model_is_swappable():
    """Without this, a project cannot point ``BILLING_SCOPE_MODEL`` anywhere else."""
    assert BillingScope._meta.swappable == "BILLING_SCOPE_MODEL"


def test_the_setting_has_a_default_even_when_the_project_sets_nothing():
    """``Meta.swappable`` reads the setting with a bare ``getattr``.

    ``conf.install_swappable_defaults`` runs at import time in ``apps.py`` so
    that it is already there; if it ever stops running, this fails rather than
    every migration command raising ``AttributeError``.
    """
    from django.conf import settings

    from vinta_billing import conf

    assert getattr(settings, conf.BILLING_SCOPE_MODEL) == conf.DEFAULT_SCOPE_MODEL
    assert conf.get_scope_model() is BillingScope


def test_the_check_constraint_is_declared_on_the_shipped_model():
    """Guards the invariant where ``save`` cannot reach."""
    conditions = [
        c.condition
        for c in BillingScope._meta.constraints
        if c.name == "billing_scope_names_a_payer"
    ]

    assert conditions == [~Q(object_id="")]
