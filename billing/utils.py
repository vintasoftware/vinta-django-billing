"""Small helpers shared across the app."""

from __future__ import annotations

from typing import Any

from django.db.models import Model
from django.http import HttpRequest


def get_request_organization(request: HttpRequest | Any) -> Model | None:
    """The organization this request is acting on, or ``None``.

    Reads, in order: whatever the tenant-scoped view mixin already resolved onto
    the request, then ``vinta-django-orgs``' middleware attribute, then the
    context variable its middleware and ``organization_context`` set. The last
    of those is what makes this work off the request path too -- in a Celery
    task wrapped in ``organization_context``, for instance.
    """
    organization = getattr(request, "organization", None)
    if organization is not None:
        return organization  # type: ignore[no-any-return]

    from organizations.middleware import get_organization
    from organizations.state import get_current_organization

    if isinstance(request, HttpRequest):
        organization = get_organization(request)
        if organization is not None:
            return organization

    return get_current_organization()
