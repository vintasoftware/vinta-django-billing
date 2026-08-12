"""The open sets that replaced the closed enums."""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from billing.constants import LimitKind, LimitRemedy
from billing.registry import EntitlementRegistry, ResourceRegistry, resources


def noop_counter(context):
    return {}


@pytest.fixture
def registry():
    """A throwaway registry, so tests never mutate the process-wide one."""
    return ResourceRegistry()


class TestResourceRegistry:
    def test_registers_and_resolves_a_resource(self, registry):
        registry.register("things", label="Things", kind=LimitKind.PREPAID, counter=noop_counter)

        assert "things" in registry
        assert registry.get("things").label == "Things"
        assert registry.counter_for("things") is noop_counter

    def test_defaults_the_remedy_when_none_is_given(self, registry):
        registry.register("things", label="Things", kind=LimitKind.PREPAID, counter=noop_counter)

        assert registry.get("things").remedy == LimitRemedy.UPGRADE_PLAN

    def test_rejects_an_unknown_kind(self, registry):
        with pytest.raises(ImproperlyConfigured, match="expected one of"):
            registry.register("things", label="Things", kind="sideways", counter=noop_counter)

    def test_rejects_an_unknown_remedy(self, registry):
        with pytest.raises(ImproperlyConfigured, match="expected one of"):
            registry.register(
                "things",
                label="Things",
                kind=LimitKind.PREPAID,
                counter=noop_counter,
                remedy="shrug",
            )

    def test_re_registering_the_identical_definition_is_allowed(self, registry):
        """A module that registers on import can legitimately be imported twice."""
        for _ in range(2):
            registry.register(
                "things", label="Things", kind=LimitKind.PREPAID, counter=noop_counter
            )

        assert len(registry) == 1

    def test_rejects_a_different_definition_under_the_same_key(self, registry):
        """Silently letting the second win makes behaviour depend on import order."""
        registry.register("things", label="Things", kind=LimitKind.PREPAID, counter=noop_counter)

        with pytest.raises(ImproperlyConfigured, match="already registered"):
            registry.register(
                "things", label="Other things", kind=LimitKind.PREPAID, counter=noop_counter
            )

    def test_unknown_key_names_what_is_registered(self, registry):
        registry.register("things", label="Things", kind=LimitKind.PREPAID, counter=noop_counter)

        with pytest.raises(ImproperlyConfigured, match="things"):
            registry.get("nope")

    def test_choices_keep_registration_order(self, registry):
        for key in ("c", "a", "b"):
            registry.register(key, label=key.upper(), kind=LimitKind.PREPAID, counter=noop_counter)

        assert [key for key, _ in registry.choices()] == ["c", "a", "b"]

    def test_of_kind_filters(self, registry):
        registry.register("pre", label="Pre", kind=LimitKind.PREPAID, counter=noop_counter)
        registry.register("post", label="Post", kind=LimitKind.POSTPAID, counter=noop_counter)

        assert [d.key for d in registry.of_kind(LimitKind.POSTPAID)] == ["post"]


class TestMeteredKey:
    def test_uses_the_configured_key(self):
        assert resources.metered_key() == "event_occurrences"

    @override_settings(VINTA_BILLING={})
    def test_infers_the_sole_postpaid_resource_when_unset(self):
        """One postpaid resource is unambiguous, so no setting is required."""
        assert resources.metered_key() == "event_occurrences"

    @override_settings(VINTA_BILLING={"METERED_RESOURCE_KEY": "widgets"})
    def test_rejects_a_prepaid_resource(self):
        with pytest.raises(ImproperlyConfigured, match="Only a postpaid resource"):
            resources.metered_key()

    def test_returns_none_when_nothing_postpaid_is_registered(self):
        registry = ResourceRegistry()
        registry.register("pre", label="Pre", kind=LimitKind.PREPAID, counter=noop_counter)

        with override_settings(VINTA_BILLING={}):
            assert registry.metered_key() is None

    def test_refuses_to_guess_between_several_postpaid_resources(self):
        """Metering the wrong resource silently bills for something unused."""
        registry = ResourceRegistry()
        for key in ("a", "b"):
            registry.register(key, label=key, kind=LimitKind.POSTPAID, counter=noop_counter)

        with override_settings(VINTA_BILLING={}):
            with pytest.raises(ImproperlyConfigured, match="METERED_RESOURCE_KEY"):
                registry.metered_key()


class TestEntitlementRegistry:
    def test_registers_and_resolves(self):
        registry = EntitlementRegistry()
        registry.register("flag", label="Flag")

        assert registry.get("flag").label == "Flag"
        assert registry.choices() == [("flag", "Flag")]
