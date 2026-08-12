"""Everything this library lets a project replace, and the defaults it uses when
the project replaces nothing.

The billing engine shipped here is deliberately ignorant of what it is billing
for. It knows how to resolve a ceiling, pool usage across a subtree, meter an
occurrence and dun a failed charge; it does not know that a "resource calendar"
exists, that a seat is a membership plus a pending invitation, or that a parent
organization pays for its children. Every one of those is a project's answer,
supplied through the settings below.

    # settings.py
    VINTA_BILLING = {
        'HIERARCHY': 'myproject.billing.ParentChainHierarchy',
        'BILLING_MANAGER_PREDICATE': 'myproject.billing.is_billing_owner_or_admin',
        'NOTIFIER': 'myproject.billing.VintaSendNotifier',
    }

Resources and entitlements are *not* configured here -- they are registered at
import time against :mod:`billing.registry`, because each one carries a callable
and a label rather than a scalar.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.utils.module_loading import import_string


SETTINGS_NAME = "VINTA_BILLING"

# Resolved once and reused: ``get_setting`` is called from the enforcement path,
# which runs on every create of every limited resource.
_settings_cache: dict[str, Any] | None = None

_DEFAULTS: dict[str, Any] = {
    # How to find the organization whose subscription pays for a given one, and
    # which organizations pool their usage against it. The default treats every
    # organization as its own billing root -- correct for a flat project, and
    # the only thing that can be assumed of `vinta-django-orgs`' organization
    # model, which has no parent field. A project with a reseller hierarchy
    # points this at its own strategy; `billing.hierarchy.ParentFieldHierarchy`
    # implements the usual parent-chain walk against configurable field names.
    "HIERARCHY": "billing.hierarchy.FlatHierarchy",
    # ``(user, organization) -> bool``: may this user see and change this
    # organization's billing? The default allows any member of the organization,
    # which is the most permissive answer that is still tenant-safe. Projects
    # with a billing-owner or admin role should narrow it.
    "BILLING_MANAGER_PREDICATE": "billing.permissions.any_member_may_manage_billing",
    # Where dunning and usage-warning messages go. The default logs them and
    # drops them, so nothing silently fails in a project that has not wired a
    # transport yet; `billing.notifications.VintaSendNotifier` ships for
    # projects using vintasend.
    "NOTIFIER": "billing.notifications.LoggingNotifier",
    # Supplies the occurrences `MeteringService` bills for. Left unset the meter
    # has nothing to count and every metering run is a no-op, which is correct
    # for a project that bills only on prepaid resources.
    "OCCURRENCE_SOURCE": None,
    # Which registered resource the meter writes occurrences against. Must name
    # a resource registered with `kind=LimitKind.POSTPAID`. Left unset, the
    # engine uses the single registered postpaid resource if there is exactly
    # one, and refuses to guess if there are several -- billing the wrong
    # resource is worse than failing loudly.
    "METERED_RESOURCE_KEY": None,
    # ``(organization) -> list[user_id]``: who is told when a charge fails or a
    # limit is approached. The default is every member of the organization,
    # since `vinta-django-orgs` has no notion of a billing owner. Projects with
    # roles should narrow it -- an "your card failed" message to every member of
    # a large organization is noise at best.
    "BILLING_RECIPIENTS": "billing.recipients.all_members",
    # URL namespace the shipped routes are mounted under. The provider
    # adapters `reverse()` their own webhook callback URLs through it, so it has
    # to match wherever the project actually included them. Set to "" when they
    # are mounted without a namespace.
    "URL_NAMESPACE": "billing",
    # Absolute base the webhook callback URLs are built against, e.g.
    # "https://api.example.com". Providers call back from outside, so a relative
    # path is not enough. Falls back to a `SITE_DOMAIN` setting for projects
    # that already have one.
    "SITE_DOMAIN": None,
    # Three-letter ISO code stamped on money the project does not qualify.
    "DEFAULT_CURRENCY": "USD",
    # How long a subscription stays in `grace` after a failed renewal before the
    # dunning schedule restricts it.
    "GRACE_PERIOD_DAYS": 7,
    # Fractions of the effective limit at which `UsageWarningService` emits its
    # "approaching" warning. Reached-the-limit warnings are not configurable:
    # they fire at the ceiling.
    "USAGE_WARNING_THRESHOLD": 0.8,
    # Per-provider credentials and options, keyed by provider slug:
    #     {'stripe': {'API_KEY': ..., 'WEBHOOK_SECRET': ...}}
    "PROVIDERS": {},
}


def _build_settings() -> dict[str, Any]:
    overrides = getattr(settings, SETTINGS_NAME, {})
    unknown = set(overrides) - set(_DEFAULTS)
    if unknown:
        # A typo in a settings key is otherwise completely silent -- the
        # library goes on using its default and the project believes it
        # configured something.
        raise ValueError(
            "Unknown %s key(s): %s. Valid keys: %s"
            % (SETTINGS_NAME, ", ".join(sorted(unknown)), ", ".join(sorted(_DEFAULTS)))
        )
    return {**_DEFAULTS, **overrides}


def get_setting(name: str) -> Any:
    """Return the resolved value of ``name``.

    ``Any`` deliberately: the dictionary is heterogeneous, and each caller knows
    which key it asked for.
    """
    global _settings_cache

    if _settings_cache is None:
        _settings_cache = _build_settings()

    return _settings_cache[name]


def get_object_from_setting(name: str) -> Any:
    """Import and return the object a dotted-path setting names.

    Returns ``None`` when the setting is unset, so optional hooks
    (``OCCURRENCE_SOURCE``) read the same way as required ones.
    """
    path = get_setting(name)
    if path is None:
        return None
    if not isinstance(path, str):
        # Tests and programmatic configuration pass the object itself.
        return path
    return import_string(path)


@receiver(setting_changed)
def _reset_settings_cache(sender: Any, setting: str, **kwargs: Any) -> None:
    """Drop the cache when ``override_settings`` touches our configuration."""
    if setting == SETTINGS_NAME:
        global _settings_cache
        _settings_cache = None
