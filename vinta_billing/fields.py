"""Serializer fields backed by the open registries.

A plain ``ChoiceField(choices=resource_choices())`` cannot work here: serializer
classes are built at import time, and a project registers its resources from
``AppConfig.ready()``, which runs later. The field would freeze an empty choice
list and then reject every value the project registered.

These resolve their choices on each access instead, so registration order stops
mattering.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from vinta_billing.registry import entitlements, resources


class _RegistryChoiceField(serializers.ChoiceField):
    """A ``ChoiceField`` whose choices are read from a registry, late."""

    registry: Any = None

    def __init__(self, **kwargs: Any) -> None:
        # DRF requires *something* here; the property below replaces it before
        # anything reads it.
        kwargs.setdefault("choices", [])
        super().__init__(**kwargs)

    # Overriding a writeable attribute with a property is exactly the intent:
    # the registry is the authority, and it is not populated yet when DRF
    # assigns `self.choices` in `__init__`.
    @property
    def choices(self) -> dict[str, str]:  # type: ignore[override]
        return dict(self.registry.choices())

    @choices.setter
    def choices(self, value: Any) -> None:
        # `ChoiceField.__init__` assigns to `self.choices`. Swallowed: the
        # registry is the authority, not whatever was passed in.
        pass

    @property
    def grouped_choices(self) -> dict[str, str]:  # type: ignore[override]
        # DRF renders the browsable-API form from this one. Flat rather than
        # grouped: the registries have no notion of option groups.
        return self.choices

    @grouped_choices.setter
    def grouped_choices(self, value: Any) -> None:
        # Assigned by `ChoiceField._set_choices`, and discarded for the same
        # reason as `choices` above.
        pass

    def to_internal_value(self, data: Any) -> Any:
        # `ChoiceField` matches against `self.choice_strings_to_values`, which
        # it builds once in `__init__` -- stale for the same reason the choices
        # were. Rebuilt per call against the live registry.
        self.choice_strings_to_values = {str(key): key for key in self.choices}
        return super().to_internal_value(data)


class ResourceKeyField(_RegistryChoiceField):
    """Accepts any key registered in :data:`vinta_billing.registry.resources`."""

    registry = resources


class EntitlementKeyField(_RegistryChoiceField):
    """Accepts any key registered in :data:`vinta_billing.registry.entitlements`."""

    registry = entitlements
