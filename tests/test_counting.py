"""The plumbing every project-supplied usage counter is built on."""

import pytest

from tests.testapp.models import Widget
from vinta_billing.counting import UsageContext, count_by_scope, merge_breakdowns


pytestmark = pytest.mark.django_db


class TestCountByOrganization:
    def test_groups_rows_per_organization(self, scope, other_scope):
        Widget.objects.create(scope=scope, name="a")
        Widget.objects.create(scope=scope, name="b")
        Widget.objects.create(scope=other_scope, name="c")

        counts = count_by_scope(Widget.objects.all())

        assert counts == {scope.pk: 2, other_scope.pk: 1}

    def test_omits_organizations_with_no_rows(self, scope, other_scope):
        """Absent, not present-with-zero: the contract every counter promises."""
        Widget.objects.create(scope=scope, name="a")

        counts = count_by_scope(Widget.objects.all())

        assert other_scope.pk not in counts

    def test_clears_the_callers_ordering(self, scope):
        """An ordered queryset would split one scope across many groups.

        Django appends `ORDER BY` columns to `GROUP BY`, so without the
        `order_by()` reset each distinct `name` would become its own group and
        the comprehension would keep only the last -- under-reporting usage,
        which in a billing engine means under-charging.
        """
        for name in ("a", "b", "c"):
            Widget.objects.create(scope=scope, name=name)

        counts = count_by_scope(Widget.objects.order_by("name"))

        assert counts == {scope.pk: 3}

    def test_an_empty_queryset_counts_nothing(self, scope):
        assert count_by_scope(Widget.objects.none()) == {}


class TestMergeBreakdowns:
    def test_adds_counts_key_wise(self):
        """The same scope in both maps must be summed, not overwritten."""
        assert merge_breakdowns({1: 2, 2: 1}, {1: 3}) == {1: 5, 2: 1}

    def test_merging_nothing_is_empty(self):
        assert merge_breakdowns() == {}


class TestUsageContext:
    def test_get_tolerates_absent_extra(self):
        context = UsageContext(scope_ids=[1])

        assert context.get("anything") is None
        assert context.get("anything", "fallback") == "fallback"

    def test_get_reads_extra(self):
        context = UsageContext(scope_ids=[1], extra={"key": 7})

        assert context.get("key") == 7
