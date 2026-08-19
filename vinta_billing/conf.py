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
        'NOTIFIER': 'myproject.billing.Notifier',
    }

Resources and entitlements are *not* configured here -- they are registered at
import time against :mod:`vinta_billing.registry`, because each one carries a callable
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

# The ``VINTA_BILLING`` object ``_settings_cache`` was built from, so a swap that
# never reached the ``setting_changed`` receiver is still noticed. See
# ``get_setting``.
_settings_cache_source: Any = None

_DEFAULTS: dict[str, Any] = {
    # How to find the organization whose subscription pays for a given one, and
    # which organizations pool their usage against it. The default treats every
    # organization as its own billing root -- correct for a flat project, and
    # the only thing that can be assumed of `vinta-django-orgs`' organization
    # model, which has no parent field. A project with a reseller hierarchy
    # points this at its own strategy; `vinta_billing.hierarchy.ParentFieldHierarchy`
    # implements the usual parent-chain walk against configurable field names.
    "HIERARCHY": "vinta_billing.hierarchy.FlatHierarchy",
    # ``(user, organization) -> bool``: may this user see and change this
    # organization's billing? The default allows any member of the organization,
    # which is the most permissive answer that is still tenant-safe. Projects
    # with a billing-owner or admin role should narrow it.
    "BILLING_MANAGER_PREDICATE": "vinta_billing.permissions.any_member_may_manage_billing",
    # Where dunning and usage-warning messages go. The default logs them and
    # drops them, so nothing silently fails in a project that has not wired a
    # transport yet. No adapter for any specific transport ships here -- see
    # `vinta_billing.notifications` for the one method a notifier must have.
    "NOTIFIER": "vinta_billing.notifications.LoggingNotifier",
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
    "BILLING_RECIPIENTS": "vinta_billing.recipients.all_members",
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
    # How long a subscription stays in `grace` after a failed renewal before
    # the dunning ladder restricts it. A plan may override it per plan through
    # `BillingPlan.grace_period_days`; this is the fallback when it does not.
    # A project carrying `BILLING_DEFAULT_GRACE_PERIOD_DAYS` over from the
    # origin application can leave it defined -- it is read as a fallback.
    "GRACE_PERIOD_DAYS": 7,
    # Fractions of the effective limit at which `UsageWarningService` emits its
    # "approaching" warning. Reached-the-limit warnings are not configurable:
    # they fire at the ceiling.
    "USAGE_WARNING_THRESHOLD": 0.8,
    # ``(job, *args) -> None``: how a sweep hands each per-subscription job
    # over. The default runs it inline, which is correct for a cron-driven
    # project and for tests, and wrong for a large installation -- point this at
    # a queueing dispatcher there. See `vinta_billing.jobs`.
    "JOB_DISPATCHER": None,
    # Per-provider credentials and options, keyed by provider slug. Each entry
    # is handed to that adapter's ``from_config`` (upper-case keys, lower-cased
    # into constructor keyword arguments), so the shape follows the adapter:
    #
    #     'PROVIDERS': {
    #         'stripe': {
    #             'API_KEY': env('STRIPE_SECRET_KEY'),
    #             'WEBHOOK_SECRET': env('STRIPE_WEBHOOK_SECRET'),
    #             'PUBLISHABLE_KEY': env('STRIPE_PUBLISHABLE_KEY'),
    #         },
    #         'mercadopago': {
    #             'ACCESS_TOKEN': env('MERCADOPAGO_ACCESS_TOKEN'),
    #             'WEBHOOK_SECRET': env('MERCADOPAGO_WEBHOOK_SECRET'),
    #             'PUBLIC_KEY': env('MERCADOPAGO_PUBLIC_KEY'),
    #         },
    #     }
    #
    # A provider absent from this dictionary is still *registered* -- its
    # inbound webhook route keeps working -- but reports ``is_configured``
    # False, so every outbound call site refuses it loudly instead of
    # authenticating with an empty credential.
    "PROVIDERS": {},
    # Mixed in front of every tenant-scoped viewset this package mounts. The
    # default is this package's own mixin, which every one of those viewsets
    # already inherits -- so the default changes nothing and `get_routes()`
    # hands back the very classes it always did.
    #
    # A project whose DRF surface resolves the acting organization its own way
    # (a header its clients already send, a URL segment, a membership lookup
    # its own refusal bodies are written against) points this at its mixin
    # instead, and mounts the shipped routes as they are rather than
    # subclassing seven viewsets to mix it in by hand. See
    # `vinta_billing.view_mixins.apply_view_mixin` for what "in front of"
    # means and what it does about the method name both mixins spell.
    "VIEW_MIXIN": "vinta_billing.view_mixins.TenantScopedViewMixin",
    # Where the shipped views and the admin get their services. Names either a
    # module or an object; either way the lookup for `payment_service` is
    # `container.get_payment_service()` if that exists and `container
    # .payment_service()` otherwise -- the second spelling being what a
    # `dependency_injector` container offers, so one can be pointed at
    # directly.
    #
    # The default is this package's own container module, which is right for a
    # project that runs no DI framework and is what every call site fell back
    # to before this setting existed. A project that *does* run a container
    # points this at it, and its own factories -- and anything a test
    # overrode on them -- are what the shipped views build. Passing a service
    # to a viewset's constructor still wins over both.
    "SERVICE_CONTAINER": "vinta_billing.services.container",
    # Which provider governs an organization's charges when its billing profile
    # carries no pin of its own. Empty rather than a guess at "stripe": a
    # library must not pick which payment processor a project charges through.
    # Empty is also the correct state for a project whose organizations are all
    # on a free plan -- the pin is written on the first confirmed charge, not
    # before.
    "DEFAULT_PROVIDER": "",
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
    global _settings_cache, _settings_cache_source

    source = getattr(settings, SETTINGS_NAME, None)
    # Identity, not equality: the check runs on the enforcement path and has to
    # stay a pointer comparison. It is a *second* line of defence behind the
    # ``setting_changed`` receiver below, not a replacement for it -- the signal
    # covers ``override_settings`` and plain attribute assignment, and this
    # covers everything that replaces the settings object without going through
    # either (``settings.configure()`` in a script, a harness swapping
    # ``settings._wrapped``). A stale billing setting is the kind of bug that
    # only shows up as a wrong charge, so it is worth one pointer comparison.
    #
    # What neither catches: *mutating* the ``VINTA_BILLING`` dict in place. The
    # object is the same one and no signal fires, so a project doing that has to
    # reset explicitly.
    if _settings_cache is None or source is not _settings_cache_source:
        _settings_cache = _build_settings()
        _settings_cache_source = source

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


def get_provider_config(slug: str) -> dict[str, Any]:
    """The ``PROVIDERS`` entry for ``slug``, or an empty mapping.

    Empty is a legitimate answer, not an error: a deployment that charges
    through one provider still has the other registered so its inbound webhook
    route resolves, and the adapter reports itself unconfigured.
    """
    return dict(get_setting("PROVIDERS").get(slug, {}))


def get_site_domain() -> str | None:
    """Absolute host the provider callback URLs are built against.

    Reads ``VINTA_BILLING['SITE_DOMAIN']`` first and falls back to a top-level
    ``SITE_DOMAIN`` setting, which many projects already define for other
    reasons.
    """
    return get_setting("SITE_DOMAIN") or getattr(settings, "SITE_DOMAIN", None)


@receiver(setting_changed)
def _reset_settings_cache(sender: Any, setting: str, **kwargs: Any) -> None:
    """Drop the cache when ``override_settings`` touches our configuration."""
    if setting == SETTINGS_NAME:
        global _settings_cache, _settings_cache_source
        _settings_cache = None
        _settings_cache_source = None
