"""Notification contexts for the dunning ladder's in-app and email notifications.

Contexts are registered through vintasend's ``@register_context`` decorator,
which registers on import. ``BillingConfig.ready()`` imports this module so the
contexts exist before the first notification renders.

vintasend is an optional dependency, so the decorator degrades to a no-op when
it is not installed: the context functions stay importable and callable, they
are simply not registered with a transport that is not there. A project using a
different notifier calls them directly, or ignores them.

Deliberately plain -- these values are passed in by the services, which already
have the ``Subscription``/``Organization`` in hand and would otherwise re-query
them.
"""

from collections.abc import Callable
from typing import Any, TypeVar

from billing.registry import resources


F = TypeVar("F", bound=Callable[..., Any])

try:
    from vintasend.services.notification_service import register_context
except ImportError:  # pragma: no cover - exercised only without the extra

    def register_context(context_name: str) -> Callable[[F], F]:
        """No-op stand-in used when vintasend is not installed."""

        def decorator(func: F) -> F:
            return func

        return decorator


def _resource_label(resource_key: str) -> str:
    """The human label for a resource key, falling back to the key itself.

    A key can legitimately be missing here: the warning that named it was
    queued while the resource was registered, and the deploy that renders it
    may have dropped that registration. Rendering the raw key is a far better
    outcome than raising inside a notification.
    """
    if resource_key in resources:
        return resources.get(resource_key).label
    return resource_key


@register_context("dunning_entered_grace_context")
def dunning_entered_grace_context(
    organization_name: str, grace_period_ends_at: str, **kwargs: Any
) -> dict[str, Any]:
    """Context for the notice sent once, when a subscription enters GRACE.

    Shared by both the in-app notification and the "payment failed" email --
    same facts, two renderings.
    """
    return {
        "organization_name": organization_name,
        "grace_period_ends_at": grace_period_ends_at,
        **kwargs,
    }


@register_context("dunning_reminder_context")
def dunning_reminder_context(
    organization_name: str,
    grace_period_ends_at: str,
    urgency: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Context for the escalating reminder email ``process_dunning`` sends on each
    retry across the grace window.

    :param urgency: ``"reminder"`` while more than a day remains before
        ``grace_period_ends_at``, ``"final_warning"`` on the last day -- the
        ladder's escalation, read by the template to change its tone/subject.
    """
    return {
        "organization_name": organization_name,
        "grace_period_ends_at": grace_period_ends_at,
        "urgency": urgency,
        **kwargs,
    }


@register_context("dunning_restricted_context")
def dunning_restricted_context(organization_name: str, **kwargs: Any) -> dict[str, Any]:
    """Context for the notice sent once, when the grace period expires unresolved
    and the subscription moves to RESTRICTED."""
    return {"organization_name": organization_name, **kwargs}


@register_context("approaching_limit_context")
def approaching_limit_context(
    organization_name: str,
    resource_key: str,
    current_usage: int,
    limit_value: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Context for the in-app notice ``UsageWarningService`` sends once per
    resource per billing cycle when usage crosses
    ``usage_warning_service.APPROACHING_LIMIT_THRESHOLD`` (default 80%) of the
    resource's effective limit, without yet being at or over it."""
    return {
        "organization_name": organization_name,
        "resource_key": resource_key,
        "resource_label": _resource_label(resource_key),
        "current_usage": current_usage,
        "limit_value": limit_value,
        **kwargs,
    }


@register_context("limit_reached_context")
def limit_reached_context(
    organization_name: str,
    resource_key: str,
    current_usage: int,
    limit_value: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Context for the in-app notice ``UsageWarningService`` sends once per
    resource per billing cycle once usage is at or over the resource's
    effective limit."""
    return {
        "organization_name": organization_name,
        "resource_key": resource_key,
        "resource_label": _resource_label(resource_key),
        "current_usage": current_usage,
        "limit_value": limit_value,
        **kwargs,
    }
