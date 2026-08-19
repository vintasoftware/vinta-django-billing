"""Route declarations, and the router that turns them into URL patterns.

The shipped endpoints are offered as a list of route dictionaries rather than a
ready-made ``urls.py`` so a project can mount them under its own prefix, drop
the ones it does not want, or register them on a router it already has:

    # myproject/urls.py
    from vinta_billing.routing import billing_router, get_extra_patterns

    urlpatterns = [
        path('api/', include(billing_router().urls)),
        *get_extra_patterns(),
    ]

Both halves are needed: the endpoints a router cannot express -- the singleton
payment-provider reads, and the two inbound provider webhooks, which carry the
provider slug in the URL -- are bound by ``get_extra_patterns`` instead. See its
docstring.

Nothing here assumes the regex router. ``register_routes`` mounts the shipped
viewsets on whatever router a project already has, including one built with
``use_regex_path=False``, and ``billing_router`` takes the same flag.
"""

from __future__ import annotations

from typing import TypedDict

from django.urls import URLPattern, path, re_path
from rest_framework.routers import DefaultRouter, SimpleRouter
from rest_framework.viewsets import ViewSetMixin

from vinta_billing.billing_views import (
    AddOnViewSet,
    BillingPeriodViewSet,
    BillingPlanViewSet,
    BillingUsageViewSet,
    MeteredOccurrenceViewSet,
    SubscriptionViewSet,
)
from vinta_billing.view_mixins import apply_view_mixin
from vinta_billing.views import (
    BillingProfileViewSet,
    DefaultPaymentProviderView,
    PaymentProviderViewSet,
    PaymentsViewSet,
)


class RouteDict(TypedDict):
    """One viewset, and where it is mounted."""

    regex: str
    viewset: type[ViewSetMixin]
    basename: str


def get_routes() -> list[RouteDict]:
    """Every viewset this app ships that a router can mount, in registration order.

    ``PaymentsViewSet`` is deliberately absent from 0.4.0 on: both of its actions
    carry the provider slug as a URL segment, which a router can only render in
    its own mode. They are bound by :func:`get_extra_patterns` instead -- see
    that docstring, and mount both halves.

    Every tenant-scoped viewset here is passed through
    :func:`vinta_billing.view_mixins.apply_view_mixin` first, so a project that
    configured ``VINTA_BILLING['VIEW_MIXIN']`` mounts these routes as they are
    instead of subclassing each viewset to mix its own scoping in. Under the
    default that function returns the same class objects, so this table is
    unchanged for a project that configured nothing.
    """
    return [
        {
            "regex": r"billing-profile",
            "viewset": apply_view_mixin(BillingProfileViewSet),
            "basename": "BillingProfile",
        },
        {
            "regex": r"billing/plans",
            "viewset": apply_view_mixin(BillingPlanViewSet),
            "basename": "BillingPlan",
        },
        {
            "regex": r"billing/usage",
            "viewset": apply_view_mixin(BillingUsageViewSet),
            "basename": "BillingUsage",
        },
        {
            "regex": r"billing/usage/periods",
            "viewset": apply_view_mixin(BillingPeriodViewSet),
            "basename": "BillingUsagePeriod",
        },
        {
            "regex": r"billing/usage/occurrences",
            "viewset": apply_view_mixin(MeteredOccurrenceViewSet),
            "basename": "BillingUsageOccurrence",
        },
        {
            "regex": r"billing/subscription",
            "viewset": apply_view_mixin(SubscriptionViewSet),
            "basename": "BillingSubscription",
        },
        {
            "regex": r"billing/add-ons",
            "viewset": apply_view_mixin(AddOnViewSet),
            "basename": "BillingAddOn",
        },
    ]


def get_extra_patterns(trailing_slash: bool = True) -> list[URLPattern]:
    """Endpoints bound directly rather than through the router.

    The payment-provider endpoints are singletons -- one per organization, with
    no list and no primary key -- so they are bound to explicit paths instead of
    being given a router prefix that implies a collection.

    The two inbound provider webhooks are here for a different reason. They carry
    the provider slug as a URL segment, and a DRF ``@action`` can spell that
    segment only one way. Written as a regex (``(?P<provider>[^/.]+)``) it is
    emitted literally by a router built with ``use_regex_path=False``; written as
    a path converter (``<str:provider>``) it is emitted literally by the default
    regex router. Either spelling therefore hands half of the projects mounting
    these a route that can never match, and a project should not have to change
    its router's mode -- a project-wide decision affecting its own URLs -- to
    receive a payment webhook. Binding them here with ``re_path`` takes them out
    of the router's hands: this is an ordinary URL pattern, and Django mixes
    ``path()`` and ``re_path()`` in one urlconf without caring which the
    neighbouring routes used. Both paths now live under ``billing/``, matching
    every other route this package serves instead of sitting apart at
    ``payments/``; the reversible names are unchanged, so a caller reversing
    them by name -- which is how both shipped MercadoPago adapters build their
    ``notification_url`` -- needs no change.

    :param trailing_slash: Whether these paths end in ``/``. Match whatever the
        router they are mounted alongside was built with: the webhook patterns
        used to come out of that router and inherited its choice.
    """
    slash = "/" if trailing_slash else ""
    return [
        re_path(
            r"^billing/payments/(?P<pk>[^/.]+)/payment-update/(?P<provider>[^/.]+)" + slash + "$",
            PaymentsViewSet.as_view({"post": "payment_update"}, detail=True, basename="Payments"),
            name="Payments-payment-update",
        ),
        re_path(
            r"^billing/payments/(?P<pk>[^/.]+)/subscription-payment-update/(?P<provider>[^/.]+)"
            + slash
            + "$",
            PaymentsViewSet.as_view(
                {"post": "subscription_payment_update"}, detail=True, basename="Payments"
            ),
            name="Payments-subscription-payment-update",
        ),
        path(
            "billing/payment-provider" + slash,
            # Tenant-scoped, so it takes the configured view mixin like every
            # viewset `get_routes` returns. The two webhooks above do not: a
            # provider posts to them with no session and no member, and their
            # tenancy comes from the payment row the URL names.
            apply_view_mixin(PaymentProviderViewSet).as_view({"get": "retrieve_provider"}),
            name="payment-provider",
        ),
        path(
            "billing/payment-provider/default" + slash,
            DefaultPaymentProviderView.as_view(),
            name="payment-provider-default",
        ),
    ]


def register_routes(router: SimpleRouter) -> SimpleRouter:
    """Register every shipped viewset on an existing router.

    The router's own mode is respected: every prefix here is a plain literal and
    every ``@action`` carries a plain ``url_path``, so the only parameterised
    segments a router renders for these viewsets are its own detail lookups --
    which it already spells in whichever form it was built with.
    """
    for route in get_routes():
        router.register(route["regex"], route["viewset"], basename=route["basename"])
    return router


def billing_router(use_regex_path: bool = True) -> DefaultRouter:
    """A router carrying only this app's endpoints.

    :param use_regex_path: Passed to ``DefaultRouter``. Defaults to ``True``,
        DRF's own default, so an existing project's URLs do not move. Pass
        ``False`` for path-converter routes (``<str:pk>`` rather than
        ``(?P<pk>[^/.]+)``) to match a project that already builds its routers
        that way.
    """
    return register_routes(DefaultRouter(use_regex_path=use_regex_path))  # type: ignore[return-value]
