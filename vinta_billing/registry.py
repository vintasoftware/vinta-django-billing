"""The open sets of things a plan can limit and grant.

In the application this library was extracted from, the limited resources and
the feature gates were two ``TextChoices`` classes -- a closed set naming
calendars, webhook subscriptions and API system users. None of that can ship in
a generic package: the whole point of the engine is that it does not know what
it is billing for.

So both become registries. A project declares its resources once, at import
time, and the engine discovers them:

    # myproject/billing_resources.py
    from vinta_billing.registry import resources
    from vinta_billing.constants import LimitKind, LimitRemedy
    from vinta_billing.counting import count_by_organization

    def count_seats(context):
        return count_by_organization(
            Membership.objects.filter(organization_id__in=context.organization_ids)
        )

    resources.register(
        'seats',
        label=_('Seats'),
        kind=LimitKind.PREPAID,
        counter=count_seats,
        remedy=LimitRemedy.UPGRADE_PLAN,
    )

Registration has to happen before the first limit check and before any form or
admin page renders a resource dropdown. Do it from your ``AppConfig.ready()``,
which is the only hook guaranteed to run after the app registry is populated and
before anything serves a request.

**On migrations.** The model fields that store a resource or entitlement key take
their ``choices`` from these registries through a callable
(:func:`resource_choices` / :func:`entitlement_choices`). Django serializes the
callable by reference rather than its result, so registering a new resource
never makes ``makemigrations`` want to write a migration -- which is the
behaviour you want from an open set, and the opposite of what a ``TextChoices``
class would have given.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver


if TYPE_CHECKING:
    from vinta_billing.counting import UsageContext


#: A usage counter answers "how much of this resource is each organization
#: using?" as ``{organization_id: count}``.
#:
#: Organizations at zero must be *absent* from the mapping rather than present
#: with a zero -- ``GROUP BY`` never emits a row for them, and the engine relies
#: on that rather than making every counter remember to strip them.
UsageCounter = Callable[["UsageContext"], dict[int, int]]


@dataclass(frozen=True)
class ResourceDefinition:
    """One thing a plan can put a ceiling on."""

    key: str
    label: str
    kind: str
    counter: UsageCounter
    remedy: str

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class EntitlementDefinition:
    """One boolean feature gate a plan can grant."""

    key: str
    label: str

    def __str__(self) -> str:
        return self.key


DefinitionT = TypeVar("DefinitionT", ResourceDefinition, EntitlementDefinition)


class Registry(Generic[DefinitionT]):
    """An ordered, name-checked set of definitions.

    Ordered because the ``choices`` these produce end up in admin dropdowns and
    API payloads, and a set's iteration order would reshuffle them between
    processes.
    """

    #: Named in the error messages, so a project sees "resource" rather than
    #: "definition".
    kind_name = "definition"

    def __init__(self) -> None:
        self._definitions: dict[str, DefinitionT] = {}

    def _add(self, definition: DefinitionT) -> DefinitionT:
        existing = self._definitions.get(definition.key)
        if existing is not None and existing != definition:
            # Re-registering the identical definition is allowed: a module that
            # registers on import can legitimately be imported twice. Changing
            # one under the same key is not -- the second registration would
            # silently win, and which one that is depends on import order.
            raise ImproperlyConfigured(
                "A different %s is already registered under %r." % (self.kind_name, definition.key)
            )
        self._definitions[definition.key] = definition
        return definition

    def get(self, key: str) -> DefinitionT:
        try:
            return self._definitions[key]
        except KeyError:
            raise self._unknown(key) from None

    def _unknown(self, key: str) -> ImproperlyConfigured:
        return ImproperlyConfigured(
            "No %s registered under %r. Registered: %s. Registration happens at "
            "import time -- see vinta_billing.registry."
            % (self.kind_name, key, ", ".join(self.keys()) or "(none)")
        )

    def __contains__(self, key: object) -> bool:
        return key in self._definitions

    def __iter__(self) -> Iterator[DefinitionT]:
        return iter(self._definitions.values())

    def __len__(self) -> int:
        return len(self._definitions)

    def keys(self) -> Sequence[str]:
        return list(self._definitions)

    def choices(self) -> list[tuple[str, str]]:
        """``[(key, label)]``, for a model field or a serializer."""
        return [(d.key, d.label) for d in self]

    def clear(self) -> None:
        """Empty the registry. For tests; see :func:`vinta_billing.testing.registered`."""
        self._definitions.clear()


class ResourceRegistry(Registry[ResourceDefinition]):
    kind_name = "resource"

    def register(
        self,
        key: str,
        *,
        label: str,
        kind: str,
        counter: UsageCounter,
        remedy: str = "",
    ) -> ResourceDefinition:
        """Declare that plans may limit ``key``, and how to count it.

        ``kind`` is a :class:`vinta_billing.constants.LimitKind` -- whether the
        resource is capped up front or metered and billed afterwards. ``remedy``
        is the :class:`vinta_billing.constants.LimitRemedy` the over-limit error tells
        the client about, so it can route the user to the right screen.
        """
        from vinta_billing.constants import LimitKind, LimitRemedy

        if kind not in LimitKind.values:
            raise ImproperlyConfigured(
                "Resource %r has kind %r; expected one of %s."
                % (key, kind, ", ".join(LimitKind.values))
            )
        if remedy and remedy not in LimitRemedy.values:
            raise ImproperlyConfigured(
                "Resource %r has remedy %r; expected one of %s."
                % (key, remedy, ", ".join(LimitRemedy.values))
            )
        return self._add(
            ResourceDefinition(
                key=key,
                label=label,
                kind=kind,
                counter=counter,
                remedy=remedy or LimitRemedy.UPGRADE_PLAN,
            )
        )

    def counter_for(self, key: str) -> UsageCounter:
        return self.get(key).counter

    def of_kind(self, kind: str) -> list[ResourceDefinition]:
        return [d for d in self if d.kind == kind]

    def metered_key(self) -> str | None:
        """The resource the meter writes occurrences against.

        ``METERED_RESOURCE_KEY`` when the project set it. Otherwise the single
        registered postpaid resource -- the common case, and unambiguous. With
        several postpaid resources and no setting this raises rather than
        picking one: metering the wrong resource silently bills a customer for
        something they did not use.

        ``None`` when nothing postpaid is registered, which is the ordinary
        state of a project that only caps prepaid resources.
        """
        from django.core.exceptions import ImproperlyConfigured

        from vinta_billing.conf import get_setting
        from vinta_billing.constants import LimitKind

        configured = get_setting("METERED_RESOURCE_KEY")
        if configured is not None:
            definition = self.get(configured)
            if definition.kind != LimitKind.POSTPAID:
                raise ImproperlyConfigured(
                    "METERED_RESOURCE_KEY names %r, which is registered as %s. "
                    "Only a postpaid resource can be metered." % (configured, definition.kind)
                )
            return configured

        postpaid = self.of_kind(LimitKind.POSTPAID)
        if not postpaid:
            return None
        if len(postpaid) > 1:
            raise ImproperlyConfigured(
                "%d postpaid resources are registered (%s). Set "
                "VINTA_BILLING['METERED_RESOURCE_KEY'] to say which one the "
                "meter bills." % (len(postpaid), ", ".join(d.key for d in postpaid))
            )
        return postpaid[0].key


class EntitlementRegistry(Registry[EntitlementDefinition]):
    kind_name = "entitlement"

    def register(self, key: str, *, label: str) -> EntitlementDefinition:
        """Declare that plans may grant ``key``."""
        return self._add(EntitlementDefinition(key=key, label=label))


#: The process-wide registries. Module-level singletons rather than something
#: hung off the app config, because model fields and module-level counters reach
#: for them at import time.
resources = ResourceRegistry()
entitlements = EntitlementRegistry()


def resource_choices() -> list[tuple[str, str]]:
    """``choices`` for a field storing a resource key. Passed by reference."""
    return resources.choices()


def entitlement_choices() -> list[tuple[str, str]]:
    """``choices`` for a field storing an entitlement key. Passed by reference."""
    return entitlements.choices()


@receiver(setting_changed)
def _reset_registries(sender: Any, setting: str, **kwargs: Any) -> None:
    """Re-run project registration when ``INSTALLED_APPS`` is overridden.

    ``override_settings(INSTALLED_APPS=...)`` rebuilds the app registry and
    re-runs every ``AppConfig.ready()``. Registration is idempotent for an
    unchanged definition, so nothing has to be cleared here -- but a project
    that swapped in a *different* app would otherwise collide with the
    definitions the previous one left behind.
    """
    if setting == "INSTALLED_APPS":
        resources.clear()
        entitlements.clear()
