"""Resolving the acting organization for a DRF request.

``vinta-django-orgs`` already resolves an organization per request -- by site
domain, by an ``Organization-Slug`` header, or from the session -- and binds it
to a context variable its middleware manages. This mixin exposes that to the
viewsets here, and re-runs the resolution after DRF authentication so a
token-authenticated caller is handled too.

The re-run matters: the organization middleware is ordinary Django middleware
and runs before DRF has authenticated anybody, so a retriever that depends on
``request.user`` sees ``AnonymousUser``. ``initial()`` runs after
authentication, which is the earliest point the user is known.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Model
from rest_framework.request import Request

from vinta_billing.utils import get_request_organization


class TenantScopedViewMixin:
    """Puts ``request.organization`` on every request the viewset serves.

    Set ``organization_required = False`` on a view that must also serve callers
    with no organization bound (a plan catalogue, say, which is the same for
    everyone).
    """

    #: When ``True`` and nothing resolves an organization, the queryset methods
    #: below return nothing rather than leaking another tenant's rows. Views
    #: that are genuinely organization-independent set this to ``False``.
    organization_required: bool = True

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)  # type: ignore[misc]
        request.organization = self.resolve_organization(request)  # type: ignore[attr-defined]

    def resolve_organization(self, request: Request) -> Model | None:
        return get_request_organization(request)

    def get_organization(self) -> Model | None:
        """The organization the current request acts on."""
        return getattr(self.request, "organization", None)  # type: ignore[attr-defined]

    def filter_queryset_by_organization(self, queryset: Any) -> Any:
        """Narrow ``queryset`` to the acting organization.

        Returns an empty queryset -- never the unfiltered one -- when no
        organization resolved and the view requires one. Failing closed is the
        only safe direction here: the alternative leaks every tenant's billing
        rows to a caller whose organization simply failed to resolve.
        """
        organization = self.get_organization()
        if organization is not None:
            return queryset.filter(organization=organization)
        if self.organization_required:
            return queryset.none()
        return queryset
