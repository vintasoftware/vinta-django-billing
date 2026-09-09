"""Resolving the scope a request is acting on.

One seam, ``VINTA_BILLING['SCOPE_RESOLVER']``, because a project's answer to
"who is being billed here?" is genuinely its own: a header its clients already
send, a URL segment, a membership lookup, a tenant middleware it already runs.

    VINTA_BILLING = {'SCOPE_RESOLVER': 'myproject.billing.resolve_scope'}

    def resolve_scope(request):
        return BillingScope.objects.scope_for(request.organization)

The shipped default handles the two cases a library can handle without guessing;
see :func:`default_scope_resolver`.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Model
from django.http import HttpRequest

from vinta_billing.conf import get_object_from_setting, get_scope_model


def default_scope_resolver(request: HttpRequest | Any) -> Model | None:
    """The shipped resolver: what the project already put there, then the caller.

    Two steps, and it stops rather than guessing at a third:

    **Whatever set ``request.scope`` first.** A project's own middleware, or a
    view mixin configured through ``VINTA_BILLING['VIEW_MIXIN']``, or an earlier
    pass of this package's own mixin. Taking it as given is what lets a project
    resolve scopes its own way without replacing this function.

    **The authenticated caller's own scope**, which is what makes a personal
    plan work out of the box -- the case this whole change exists to support.
    Asked through the scope manager's optional ``scope_for`` hook rather than by
    querying columns directly: the shipped ``BillingScope`` has a generic key, a
    project's swapped-in model may have a typed one, and only the model knows
    how to look itself up. A scope model that does not define ``scope_for``
    simply declines, and the project configures ``SCOPE_RESOLVER``.

    Returns ``None`` rather than guessing further. ``None`` is a safe answer:
    :meth:`~vinta_billing.view_mixins.TenantScopedViewMixin.filter_queryset_by_scope`
    fails closed on it, so an unresolved caller sees nothing rather than
    somebody else's billing.
    """
    scope = getattr(request, "scope", None)
    if scope is not None:
        return scope  # type: ignore[no-any-return]

    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    scope_for = getattr(get_scope_model()._default_manager, "scope_for", None)
    if scope_for is None:
        return None
    return scope_for(user)  # type: ignore[no-any-return]


def get_request_scope(request: HttpRequest | Any) -> Model | None:
    """The scope this request is acting on, or ``None``.

    Runs whatever ``SCOPE_RESOLVER`` names. Kept as a function of its own rather
    than inlined at the call sites so that the resolver is read fresh each time
    -- a test that overrides the setting must not be served a resolver bound at
    import.
    """
    resolver = get_object_from_setting("SCOPE_RESOLVER")
    return resolver(request)  # type: ignore[no-any-return]
