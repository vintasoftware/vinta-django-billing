"""Which scope pays, and whose usage counts against it.

Two questions the engine cannot answer on its own:

* **Who holds the subscription?** Only billing roots do. In a flat project every
  scope is one. In a reseller project a child scope holds no subscription and
  bills against an ancestor.
* **Whose usage pools into a ceiling?** Everything in the root's subtree, down
  to but not including any nested root, which pays for its own subtree.

Most projects are flat, and a parent walk over a table where every ``parent_id``
is NULL costs a query per pooled read to reach the answer :class:`FlatHierarchy`
gives for free -- so that stays the default. A project with a real hierarchy
configures :class:`ParentFieldHierarchy`, which since scopes carry their own
``parent`` needs no project code at all, or writes its own:

    VINTA_BILLING = {'HIERARCHY': 'vinta_billing.hierarchy.ParentFieldHierarchy'}
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from typing import Any, Protocol

from django.db.models import Model, Q

from vinta_billing.conf import get_object_from_setting
from vinta_billing.exceptions import BillingRootCycleError


class BillingHierarchy(Protocol):
    """What the engine needs to know about scope structure."""

    def is_billing_root(self, scope: Model) -> bool:
        """Does ``scope`` hold its own subscription?"""
        ...

    def resolve_billing_root(self, scope: Model) -> Model:
        """The scope whose subscription pays for ``scope``."""
        ...

    def billing_root_q(self) -> Q:
        """A filter selecting billing roots, for queries over many scopes."""
        ...

    def pooled_scope_ids(self, root: Model) -> Sequence[int]:
        """Every scope whose usage counts against ``root``'s ceiling.

        Includes ``root`` itself, and stops at any nested billing root.
        """
        ...


class FlatHierarchy:
    """Every scope is its own billing root and pools with nobody.

    The default. A flat project reaches the same answers as
    :class:`ParentFieldHierarchy` over an all-NULL ``parent`` column, without
    the descendant query each pooled read would cost.
    """

    def is_billing_root(self, scope: Model) -> bool:
        return True

    def resolve_billing_root(self, scope: Model) -> Model:
        return scope

    def billing_root_q(self) -> Q:
        # Matches every row. `Q()` rather than `Q(pk__isnull=False)` so it
        # composes into a caller's filter without adding a redundant clause.
        return Q()

    def pooled_scope_ids(self, root: Model) -> Sequence[int]:
        return [root.pk]


class ParentFieldHierarchy:
    """A parent chain, with an optional flag marking a child as its own root.

    The usual reseller shape. ``AbstractBillingScope`` ships a self-referential
    ``parent``, so this works against the shipped scope model as configured --
    no project code, no field on a model this package does not own, which is
    the situation this class was originally written to work around.

    Both field names stay configurable for a project that swapped the scope
    model and wants the chain to follow *its* tree rather than the scope tree:

        class ResellerHierarchy(ParentFieldHierarchy):
            parent_field = 'owner_org'
            root_flag_field = 'is_reseller'

    ``root_flag_field`` is ``None`` by default, which means only parentless
    scopes are roots.
    """

    parent_field = "parent"
    root_flag_field: str | None = None

    def is_billing_root(self, scope: Model) -> bool:
        """True at the top of the chain, or wherever the flag marks a new root.

        A flagged child is its own billing root -- it pays for its own subtree
        rather than pooling into a grandparent's ceiling.
        """
        if getattr(scope, "%s_id" % self.parent_field) is None:
            return True
        if self.root_flag_field is None:
            return False
        return bool(getattr(scope, self.root_flag_field))

    def resolve_billing_root(self, scope: Model) -> Model:
        """Walk up to the nearest ancestor that is a billing root.

        Cycle-guarded: ``parent`` is user-mutable data, and returning an
        arbitrary node from a cycle would silently leave every scope on
        it billing against a different root depending on where the walk
        started.
        """
        seen: set[Any] = set()
        node: Model | None = scope
        while node is not None:
            if node.pk in seen:
                raise BillingRootCycleError(scope.pk, seen)
            seen.add(node.pk)
            if self.is_billing_root(node):
                return node
            node = getattr(node, self.parent_field)
        # Unreachable: a parentless scope is always a root and returns
        # above, so the walk only continues while the parent is set. Kept as a
        # defensive fallback rather than an assert.
        return scope

    def billing_root_q(self) -> Q:
        q = Q(**{"%s__isnull" % self.parent_field: True})
        if self.root_flag_field is not None:
            q |= Q(**{self.root_flag_field: True})
        return q

    def pooled_scope_ids(self, root: Model) -> Sequence[int]:
        """Breadth-first walk down from ``root``, pruning at nested roots.

        Iterative and batched by depth rather than recursive: the subtree is
        unbounded, and one query per level keeps a deep reseller tree from
        issuing a query per scope.
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


def resolve_billing_root(scope: Model) -> Model:
    """Shorthand for ``get_hierarchy().resolve_billing_root(...)``."""
    return get_hierarchy().resolve_billing_root(scope)


def is_billing_root(scope: Model) -> bool:
    """Shorthand for ``get_hierarchy().is_billing_root(...)``."""
    return get_hierarchy().is_billing_root(scope)


def pooled_scope_ids(root: Model) -> Sequence[int]:
    """Shorthand for ``get_hierarchy().pooled_scope_ids(...)``."""
    return get_hierarchy().pooled_scope_ids(root)
