"""Which organization pays, and whose usage counts against it.

Two questions the engine cannot answer on its own:

* **Who holds the subscription?** Only billing roots do. In a flat project every
  organization is one. In a reseller project a child organization holds no
  subscription and bills against an ancestor.
* **Whose usage pools into a ceiling?** Everything in the root's subtree, down
  to but not including any nested root, which pays for its own subtree.

``vinta-django-orgs``' organization model has a name and a slug and nothing
else -- no parent, no reseller flag -- so the library cannot assume a shape. The
default here is therefore :class:`FlatHierarchy`, and a project with a real
hierarchy configures :class:`ParentFieldHierarchy` or writes its own:

    VINTA_BILLING = {'HIERARCHY': 'myproject.billing.ResellerHierarchy'}
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from typing import Any, Protocol

from django.db.models import Model, Q

from vinta_billing.conf import get_object_from_setting
from vinta_billing.exceptions import BillingRootCycleError


class BillingHierarchy(Protocol):
    """What the engine needs to know about organization structure."""

    def is_billing_root(self, organization: Model) -> bool:
        """Does ``organization`` hold its own subscription?"""
        ...

    def resolve_billing_root(self, organization: Model) -> Model:
        """The organization whose subscription pays for ``organization``."""
        ...

    def billing_root_q(self) -> Q:
        """A filter selecting billing roots, for queries over many organizations."""
        ...

    def pooled_organization_ids(self, root: Model) -> Sequence[int]:
        """Every organization whose usage counts against ``root``'s ceiling.

        Includes ``root`` itself, and stops at any nested billing root.
        """
        ...


class FlatHierarchy:
    """Every organization is its own billing root and pools with nobody.

    The default, and the only hierarchy that holds for an unmodified
    ``vinta-django-orgs`` organization model.
    """

    def is_billing_root(self, organization: Model) -> bool:
        return True

    def resolve_billing_root(self, organization: Model) -> Model:
        return organization

    def billing_root_q(self) -> Q:
        # Matches every row. `Q()` rather than `Q(pk__isnull=False)` so it
        # composes into a caller's filter without adding a redundant clause.
        return Q()

    def pooled_organization_ids(self, root: Model) -> Sequence[int]:
        return [root.pk]


class ParentFieldHierarchy:
    """A parent chain, with an optional flag marking a child as its own root.

    For projects whose organization model has a self-referential parent -- the
    usual reseller shape. Both field names are configurable, so this fits a
    model with ``parent``/``can_invite_organizations`` as readily as one with
    ``owner_org``/``is_reseller``:

        class ResellerHierarchy(ParentFieldHierarchy):
            parent_field = 'parent'
            root_flag_field = 'can_invite_organizations'

    Set ``root_flag_field`` to ``None`` when only parentless organizations are
    roots.
    """

    parent_field = "parent"
    root_flag_field: str | None = None

    def is_billing_root(self, organization: Model) -> bool:
        """True at the top of the chain, or wherever the flag marks a new root.

        A flagged child is its own billing root -- it pays for its own subtree
        rather than pooling into a grandparent's ceiling.
        """
        if getattr(organization, "%s_id" % self.parent_field) is None:
            return True
        if self.root_flag_field is None:
            return False
        return bool(getattr(organization, self.root_flag_field))

    def resolve_billing_root(self, organization: Model) -> Model:
        """Walk up to the nearest ancestor that is a billing root.

        Cycle-guarded: ``parent`` is user-mutable data, and returning an
        arbitrary node from a cycle would silently leave every organization on
        it billing against a different root depending on where the walk
        started.
        """
        seen: set[Any] = set()
        org: Model | None = organization
        while org is not None:
            if org.pk in seen:
                raise BillingRootCycleError(organization.pk, seen)
            seen.add(org.pk)
            if self.is_billing_root(org):
                return org
            org = getattr(org, self.parent_field)
        # Unreachable: a parentless organization is always a root and returns
        # above, so the walk only continues while the parent is set. Kept as a
        # defensive fallback rather than an assert.
        return organization

    def billing_root_q(self) -> Q:
        q = Q(**{"%s__isnull" % self.parent_field: True})
        if self.root_flag_field is not None:
            q |= Q(**{self.root_flag_field: True})
        return q

    def pooled_organization_ids(self, root: Model) -> Sequence[int]:
        """Breadth-first walk down from ``root``, pruning at nested roots.

        Iterative and batched by depth rather than recursive: the subtree is
        unbounded, and one query per level keeps a deep reseller tree from
        issuing a query per organization.
        """
        model = type(root)
        collected: list[int] = [root.pk]
        frontier: list[int] = [root.pk]
        seen: set[Any] = {root.pk}
        while frontier:
            children = model._default_manager.filter(
                **{"%s_id__in" % self.parent_field: frontier}
            ).exclude(self.billing_root_q())
            child_ids = [pk for pk in children.values_list("pk", flat=True) if pk not in seen]
            if not child_ids:
                break
            seen.update(child_ids)
            collected.extend(child_ids)
            frontier = child_ids
        return collected


@cache
def _instantiate(strategy: type) -> Any:
    return strategy()


def get_hierarchy() -> BillingHierarchy:
    """The configured hierarchy strategy, instantiated once.

    Cached on the class rather than on the setting, so ``override_settings``
    swapping ``HIERARCHY`` gets a fresh strategy while repeated calls under one
    configuration do not rebuild it.
    """
    strategy = get_object_from_setting("HIERARCHY")
    if isinstance(strategy, type):
        return _instantiate(strategy)  # type: ignore[no-any-return]
    # Already an instance -- a project configured an object rather than a path.
    return strategy  # type: ignore[no-any-return]


def resolve_billing_root(organization: Model) -> Model:
    """Shorthand for ``get_hierarchy().resolve_billing_root(...)``."""
    return get_hierarchy().resolve_billing_root(organization)


def is_billing_root(organization: Model) -> bool:
    """Shorthand for ``get_hierarchy().is_billing_root(...)``."""
    return get_hierarchy().is_billing_root(organization)


def pooled_organization_ids(root: Model) -> Sequence[int]:
    """Shorthand for ``get_hierarchy().pooled_organization_ids(...)``."""
    return get_hierarchy().pooled_organization_ids(root)
