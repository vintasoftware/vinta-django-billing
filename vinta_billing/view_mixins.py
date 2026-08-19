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

A project that resolves the acting organization some other way -- from a header
of its own, a URL segment, or a membership lookup with its own refusal bodies --
names its mixin in ``VINTA_BILLING['VIEW_MIXIN']``, and every tenant-scoped
viewset this package mounts is built with it in front. See
:func:`apply_view_mixin`.
"""

from __future__ import annotations

from typing import Any, TypeVar, cast

from django.db.models import Model
from rest_framework.request import Request

from vinta_billing.conf import get_object_from_setting
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
        request.organization = self.resolve_request_organization(request)  # type: ignore[attr-defined]

    def resolve_request_organization(self, request: Request) -> Model | None:
        """The organization to stamp onto the request, asked in three steps.

        **What is already there.** ``vinta-django-orgs``' middleware leaves a
        lazy organization on every request, and a mixin configured through
        ``VIEW_MIXIN`` may have resolved one in ``perform_authentication`` --
        which is the earliest point the caller is known, and where such mixins
        do their work. The truth test is what forces the middleware's lazy
        object, and forcing it here is the resolution this class was written to
        re-run: it happens after DRF authentication, so a retriever that reads
        ``request.user`` sees the real caller rather than ``AnonymousUser``.

        **This class's own hook.** :meth:`resolve_organization`, unchanged, for
        a project that overrides it to answer the question outright.

        **What that hook assigned rather than returned.** A project mixin sitting
        in front spells ``resolve_organization`` too -- ``vinta_orgs.drf
        .OrganizationScopedAPIViewMixin`` does, and its method wins on name
        resolution -- and means something different by it: it *assigns*
        ``request.organization`` and returns ``None``. Taking that ``None`` at
        face value would undo the resolution and 403 every billing endpoint, so
        the request is read again before giving up.

        Returns a real ``None`` rather than whatever falsy stand-in it was
        handed, so the ``is None`` checks downstream -- in
        :meth:`filter_queryset_by_organization`, in the shipped viewsets --
        answer the question they are asking. The middleware's lazy object is
        never ``None`` by identity even when it resolves to nothing.
        """
        organization = getattr(request, "organization", None)
        if not organization:
            organization = self.resolve_organization(request) or get_request_organization(request)
        return organization or None

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


#: Built classes, keyed by ``(mixin, viewset)``. A URL conf is walked more than
#: once -- reverse lookups, ``drf-spectacular``, a test resolving the same path
#: -- and a fresh class per call would hand out several classes with one name
#: and one route.
_MIXED_IN: dict[tuple[type, type], type] = {}

#: Bound to ``type`` so a caller keeps the class it passed in: the route table
#: and ``get_extra_patterns`` both go on to call ``as_view()`` on the result.
_ViewSetT = TypeVar("_ViewSetT", bound=type)


def get_view_mixin() -> type | None:
    """The class named by ``VINTA_BILLING['VIEW_MIXIN']``."""
    mixin: type | None = get_object_from_setting("VIEW_MIXIN")
    return mixin


def apply_view_mixin(viewset: _ViewSetT) -> _ViewSetT:
    """``viewset`` with the configured view mixin in front of it.

    Returns ``viewset`` itself -- the same class object, not a copy -- whenever
    mixing anything in would be a no-op:

    * the viewset is not tenant-scoped (the plan catalogue is the same for every
      caller, and the two inbound provider webhooks are authenticated by a
      provider signature rather than by a member of anything);
    * the configured mixin is already in its MRO, which is the case under the
      default, where ``VIEW_MIXIN`` names the very mixin those viewsets inherit.

    So a project that configures nothing mounts exactly the classes it always
    did, by identity.

    Otherwise the mixin goes *in front*: ``type(name, (mixin, viewset), ...)``.
    Ahead is the only useful position -- a project's mixin overrides
    ``perform_authentication`` or ``resolve_organization`` to do the resolving,
    and a mixin behind the viewset would lose every one of those to this
    package's own. :meth:`TenantScopedViewMixin.initial` is written for that
    ordering; see the comment in it about the method name both mixins spell.

    ``__doc__`` and ``__module__`` are carried over: ``drf-spectacular`` renders
    ``view.__doc__`` as an endpoint's description, and Python does not inherit
    it, so a generated class without it would publish an empty description for
    every billing endpoint.
    """
    mixin = get_view_mixin()
    if mixin is None or not issubclass(viewset, TenantScopedViewMixin):
        return viewset
    if mixin in viewset.__mro__:
        return viewset

    key = (mixin, viewset)
    built = _MIXED_IN.get(key)
    if built is None:
        built = type(
            viewset.__name__,
            (mixin, viewset),
            {"__doc__": viewset.__doc__, "__module__": viewset.__module__},
        )
        _MIXED_IN[key] = built
    return cast("_ViewSetT", built)
