"""Subtree pooling, against a model that actually nests.

``tests.test_hierarchy`` covers the walk-up (resolving a billing root) with
fakes, because that path only reads attributes. Pooling walks *down* and issues
real queries per level, so it needs a real model -- ``tests.testapp.Company``,
standing in for the organization model of a project whose organizations nest.
"""

import pytest

from billing.hierarchy import FlatHierarchy, ParentFieldHierarchy
from tests.testapp.models import Company


pytestmark = pytest.mark.django_db


class FlaggedHierarchy(ParentFieldHierarchy):
    parent_field = "parent"
    root_flag_field = "is_own_root"


@pytest.fixture
def tree():
    """A three-level tree with a nested reseller hanging off the root.

    root
    ├── child_a
    │   └── grandchild
    ├── child_b
    └── reseller (is_own_root)   <- pays for its own subtree
        └── reseller_child
    """
    root = Company.objects.create(name="root")
    child_a = Company.objects.create(name="child_a", parent=root)
    child_b = Company.objects.create(name="child_b", parent=root)
    grandchild = Company.objects.create(name="grandchild", parent=child_a)
    reseller = Company.objects.create(name="reseller", parent=root, is_own_root=True)
    reseller_child = Company.objects.create(name="reseller_child", parent=reseller)
    return {
        "root": root,
        "child_a": child_a,
        "child_b": child_b,
        "grandchild": grandchild,
        "reseller": reseller,
        "reseller_child": reseller_child,
    }


class TestPooling:
    def test_collects_the_whole_subtree(self, tree):
        pooled = set(FlaggedHierarchy().pooled_organization_ids(tree["root"]))

        assert pooled == {
            tree["root"].pk,
            tree["child_a"].pk,
            tree["child_b"].pk,
            tree["grandchild"].pk,
        }

    def test_prunes_at_a_nested_billing_root(self, tree):
        """A nested reseller pays for its own subtree.

        Folding it in would charge the ancestor for capacity it did not sell,
        and double-count the same usage against two ceilings.
        """
        pooled = set(FlaggedHierarchy().pooled_organization_ids(tree["root"]))

        assert tree["reseller"].pk not in pooled
        assert tree["reseller_child"].pk not in pooled

    def test_a_nested_root_pools_its_own_subtree(self, tree):
        pooled = set(FlaggedHierarchy().pooled_organization_ids(tree["reseller"]))

        assert pooled == {tree["reseller"].pk, tree["reseller_child"].pk}

    def test_a_leaf_pools_only_itself(self, tree):
        assert FlaggedHierarchy().pooled_organization_ids(tree["grandchild"]) == [
            tree["grandchild"].pk
        ]

    def test_descends_one_query_per_level_not_one_per_row(self, tree, django_assert_num_queries):
        """The subtree is unbounded, so a query per organization would not scale.

        Three for this tree: one for the root's children, one for theirs, and
        one that finds the level below empty and stops the walk.
        """
        with django_assert_num_queries(3):
            FlaggedHierarchy().pooled_organization_ids(tree["root"])

    def test_a_cycle_below_the_root_terminates(self, tree):
        """A cycle is reachable by descent once a cycle member is its own root.

        `parent` is editable in the admin, so this is not hypothetical. Without
        the `seen` set the walk never returns.
        """
        loop_top = Company.objects.create(name="loop_top", parent=tree["root"])
        loop_bottom = Company.objects.create(name="loop_bottom", parent=loop_top)
        loop_top.parent = loop_bottom
        loop_top.save(update_fields=["parent"])

        pooled = FlaggedHierarchy().pooled_organization_ids(tree["root"])

        assert len(pooled) == len(set(pooled))


class TestFlatPoolingIsNotAffectedByStructure:
    def test_ignores_children_entirely(self, tree):
        """The default hierarchy pools nothing, whatever the data looks like."""
        assert FlatHierarchy().pooled_organization_ids(tree["root"]) == [tree["root"].pk]
