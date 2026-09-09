"""Scope models a project might swap in, with columns of their own.

Two of them, because they answer two different questions and only one can be
active at a time -- ``BILLING_SCOPE_MODEL`` names one, and the other is swapped
out and has no table.

:class:`CompanyScope` is the ordinary case: a project that bills one kind of
thing and wants a real foreign key to it rather than the shipped generic key.
The suite runs end to end against it under ``tests.settings_scope_swapped``,
which is what proves no relation in ``billing`` secretly still resolves to
``vinta_billing.BillingScope``.

:class:`MixedScope` is the case the whole 0.8 change exists for: one project
selling a plan to a **user** and a plan to a **company**, side by side, with a
CHECK constraint saying a scope is exactly one of the two. ``tests.settings_mixed``
points at it.

Both carry a ``get_or_create_for`` matching the shipped manager's, because that
is the hook ``tests/conftest.py`` and
``vinta_billing.utils.default_scope_resolver`` reach for -- a project swapping
the model out and wanting either to keep working writes the same two methods.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.conf import settings
from django.db import models

from vinta_billing.constants import ScopeType
from vinta_billing.models import AbstractBillingScope


def _label_for(obj) -> str:
    """``name`` where there is one, else ``str()`` -- as the shipped manager does."""
    return str(getattr(obj, "name", None) or obj)


class CompanyScopeManager(models.Manager):
    """The two methods a swapped-in scope model is expected to offer."""

    def get_or_create_for(self, obj, *, scope_type=None, label=None, owner=None, parent=None):
        scope, created = self.get_or_create(
            company=obj,
            defaults={
                "scope_type": scope_type or ScopeType.ORGANIZATION,
                "label": label if label is not None else _label_for(obj),
                "owner": owner,
                "parent": parent,
            },
        )
        return scope, created

    def scope_for(self, obj):
        return self.filter(company=obj).first()


class CompanyScope(AbstractBillingScope):
    """A scope that is exactly one company, held by a real foreign key.

    ``PROTECT``, not ``CASCADE``: deleting a company must not delete the record
    of what it was billed.
    """

    company = models.ForeignKey(
        "testapp.Company",
        on_delete=models.PROTECT,
        related_name="billing_scopes",
    )

    objects: ClassVar[CompanyScopeManager] = CompanyScopeManager()

    class Meta(AbstractBillingScope.Meta):
        abstract = False
        swappable = "BILLING_SCOPE_MODEL"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["scope_type", "scope_key"],
                name="company_scope_unique_key_per_type",
            ),
        ]

    @property
    def scope(self) -> Any:
        return self.company

    @scope.setter
    def scope(self, value: Any) -> None:
        self.company = value

    def build_scope_key(self) -> str:
        return "" if self.company_id is None else "company:%s" % self.company_id


class MixedScopeManager(models.Manager):
    """Dispatches on what it is handed, which is the point of the model."""

    def get_or_create_for(self, obj, *, scope_type=None, label=None, owner=None, parent=None):
        is_user = isinstance(obj, _user_model())
        lookup = {"user": obj} if is_user else {"company": obj}
        scope, created = self.get_or_create(
            **lookup,
            defaults={
                "scope_type": scope_type or (ScopeType.USER if is_user else ScopeType.ORGANIZATION),
                "label": label if label is not None else _label_for(obj),
                "owner": owner if owner is not None else (obj if is_user else None),
                "parent": parent,
            },
        )
        return scope, created

    def scope_for(self, obj):
        if isinstance(obj, _user_model()):
            return self.filter(user=obj).first()
        return self.filter(company=obj).first()


def _user_model():
    from django.contrib.auth import get_user_model

    return get_user_model()


class MixedScope(AbstractBillingScope):
    """A personal plan or a team plan, in one project and one table.

    The constraint is the interesting part: exactly one of the two columns is
    set on every row, so "who pays for this?" always has one answer, and the
    database says so rather than the application promising it.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="personal_billing_scopes",
    )
    company = models.ForeignKey(
        "testapp.Company",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="team_billing_scopes",
    )

    objects: ClassVar[MixedScopeManager] = MixedScopeManager()

    class Meta(AbstractBillingScope.Meta):
        abstract = False
        swappable = "BILLING_SCOPE_MODEL"
        constraints: ClassVar = [
            models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=False, company__isnull=True)
                    | models.Q(user__isnull=True, company__isnull=False)
                ),
                name="mixed_scope_is_exactly_one_payer",
            ),
            models.UniqueConstraint(
                fields=["scope_type", "scope_key"],
                name="mixed_scope_unique_key_per_type",
            ),
        ]

    @property
    def scope(self) -> Any:
        return self.user if self.user_id is not None else self.company

    @scope.setter
    def scope(self, value: Any) -> None:
        if isinstance(value, _user_model()):
            self.user, self.company = value, None
            self.scope_type = ScopeType.USER
        else:
            self.user, self.company = None, value
            self.scope_type = ScopeType.ORGANIZATION

    def build_scope_key(self) -> str:
        if self.user_id is not None:
            return "user:%s" % self.user_id
        if self.company_id is not None:
            return "company:%s" % self.company_id
        return ""
