"""Who pays for whom.

The hierarchy strategy is the seam that lets this package run against
``vinta-django-orgs``' organization model, which has no parent field at all,
while still supporting the reseller trees the engine was extracted from.
"""

import pytest
from django.test import override_settings

from billing.exceptions import BillingRootCycleError
from billing.hierarchy import (
    FlatHierarchy,
    ParentFieldHierarchy,
    get_hierarchy,
    resolve_billing_root,
)


class FakeOrg:
    """Stands in for an organization model with a parent chain.

    A fake rather than a real model: the point is that ``ParentFieldHierarchy``
    reads nothing but the two configured attribute names, and a fake proves that
    far more directly than adding a parent field to the test app would.
    """

    def __init__(self, pk, parent=None, is_own_root=False):
        self.pk = pk
        self.parent = parent
        self.parent_id = parent.pk if parent is not None else None
        self.is_own_root = is_own_root


class FlaggedHierarchy(ParentFieldHierarchy):
    parent_field = "parent"
    root_flag_field = "is_own_root"


class TestFlatHierarchy:
    def test_is_the_default(self):
        assert isinstance(get_hierarchy(), FlatHierarchy)

    def test_every_organization_is_its_own_root(self, organization):
        assert FlatHierarchy().is_billing_root(organization) is True
        assert FlatHierarchy().resolve_billing_root(organization) is organization

    def test_nothing_pools_with_anything(self, organization):
        assert FlatHierarchy().pooled_organization_ids(organization) == [organization.pk]

    def test_root_filter_matches_everything(self, organization, other_organization):
        from vinta_orgs.conf import get_organization_model

        matched = get_organization_model().objects.filter(FlatHierarchy().billing_root_q())

        assert matched.count() == 2


class TestParentFieldHierarchy:
    def test_a_parentless_organization_is_a_root(self):
        assert ParentFieldHierarchy().is_billing_root(FakeOrg(1)) is True

    def test_a_child_is_not_a_root(self):
        root = FakeOrg(1)

        assert ParentFieldHierarchy().is_billing_root(FakeOrg(2, parent=root)) is False

    def test_a_flagged_child_is_its_own_root(self):
        """A reseller nested under another reseller pays for its own subtree."""
        root = FakeOrg(1)
        child = FakeOrg(2, parent=root, is_own_root=True)

        assert FlaggedHierarchy().is_billing_root(child) is True

    def test_resolves_through_several_levels(self):
        root = FakeOrg(1)
        middle = FakeOrg(2, parent=root)
        leaf = FakeOrg(3, parent=middle)

        assert ParentFieldHierarchy().resolve_billing_root(leaf) is root

    def test_stops_at_the_nearest_flagged_ancestor(self):
        root = FakeOrg(1)
        reseller = FakeOrg(2, parent=root, is_own_root=True)
        leaf = FakeOrg(3, parent=reseller)

        assert FlaggedHierarchy().resolve_billing_root(leaf) is reseller

    def test_a_cycle_raises_rather_than_returning_an_arbitrary_node(self):
        """`parent` is user-mutable data, so a cycle is reachable in production.

        Returning some node from the cycle would leave every organization on it
        billing against a different root depending on where the walk started --
        wrong, and invisible.
        """
        a = FakeOrg(1)
        b = FakeOrg(2, parent=a)
        a.parent = b
        a.parent_id = b.pk

        with pytest.raises(BillingRootCycleError):
            ParentFieldHierarchy().resolve_billing_root(a)


class TestConfiguredHierarchy:
    @override_settings(VINTA_BILLING={"HIERARCHY": "tests.test_hierarchy.FlaggedHierarchy"})
    def test_the_setting_selects_the_strategy(self):
        assert isinstance(get_hierarchy(), FlaggedHierarchy)

    @override_settings(VINTA_BILLING={"HIERARCHY": "tests.test_hierarchy.FlaggedHierarchy"})
    def test_the_module_level_shorthand_uses_it(self):
        root = FakeOrg(1)
        leaf = FakeOrg(2, parent=root)

        assert resolve_billing_root(leaf) is root
