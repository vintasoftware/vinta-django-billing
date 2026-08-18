"""Small helpers shared across the app."""

from __future__ import annotations

from typing import Any

from django.db.models import Model
from django.http import HttpRequest


def get_organization_state() -> Any:
    """The ``vinta-django-orgs`` context state, bound to the configured model.

    Built per call rather than once at import: ``OrganizationState`` resolves
    ``ORGANIZATION_MODEL`` in its constructor, so a module-level instance would
    both need the app registry to be populated at import time and go stale the
    moment a test points the setting somewhere else. Resolution is an app
    registry dictionary lookup, which is not worth caching against that.

    Unparameterized on purpose. The typed, project-specific subclass described
    in ``vinta-django-orgs``' documentation is exactly what a library cannot
    declare -- it would have to name the project's concrete organization class
    -- and the base class already resolves the configured model at runtime.
    """
    from vinta_orgs.state import OrganizationState

    return OrganizationState()


def get_request_organization(request: HttpRequest | Any) -> Model | None:
    """The organization this request is acting on, or ``None``.

    Reads, in order: whatever the tenant-scoped view mixin already resolved onto
    the request, then ``vinta-django-orgs``' middleware attribute, then the
    organization bound to the current context. The last of those is what makes
    this work off the request path too -- in a background job that bound one
    around its unit of work, for instance.
    """
    organization = getattr(request, "organization", None)
    if organization is not None:
        return organization  # type: ignore[no-any-return]

    from vinta_orgs.middleware import get_organization

    if isinstance(request, HttpRequest):
        organization = get_organization(request)
        if organization is not None:
            return organization

    return get_organization_state().get()  # type: ignore[no-any-return]
