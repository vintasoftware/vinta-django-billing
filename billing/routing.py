"""Route declarations, and the router that turns them into URL patterns.

The shipped endpoints are offered as a list of route dictionaries rather than a
ready-made ``urls.py`` so a project can mount them under its own prefix, drop
the ones it does not want, or register them on a router it already has:

    # myproject/urls.py
    from billing.routing import billing_router

    urlpatterns = [path('api/', include(billing_router().urls))]
"""

from __future__ import annotations

from typing import TypedDict

from django.urls import URLPattern
from rest_framework.routers import DefaultRouter, SimpleRouter
from rest_framework.viewsets import ViewSetMixin


class RouteDict(TypedDict):
    """One viewset, and where it is mounted."""

    regex: str
    viewset: type[ViewSetMixin]
    basename: str


def get_routes() -> list[RouteDict]:
    """Every viewset this app ships, in registration order."""
    from billing.billing_views import (
        AddOnViewSet,
        BillingPeriodViewSet,
        BillingPlanViewSet,
        BillingUsageViewSet,
        MeteredOccurrenceViewSet,
        SubscriptionViewSet,
    )
    from billing.views import BillingProfileViewSet, PaymentsViewSet

    return [
        {"regex": r"payments", "viewset": PaymentsViewSet, "basename": "Payments"},
        {
            "regex": r"billing-profile",
            "viewset": BillingProfileViewSet,
            "basename": "BillingProfile",
        },
        {"regex": r"billing/plans", "viewset": BillingPlanViewSet, "basename": "BillingPlan"},
        {"regex": r"billing/usage", "viewset": BillingUsageViewSet, "basename": "BillingUsage"},
        {
            "regex": r"billing/usage/periods",
            "viewset": BillingPeriodViewSet,
            "basename": "BillingUsagePeriod",
        },
        {
            "regex": r"billing/usage/occurrences",
            "viewset": MeteredOccurrenceViewSet,
            "basename": "BillingUsageOccurrence",
        },
        {
            "regex": r"billing/subscription",
            "viewset": SubscriptionViewSet,
            "basename": "BillingSubscription",
        },
        {"regex": r"billing/add-ons", "viewset": AddOnViewSet, "basename": "BillingAddOn"},
    ]


def get_extra_patterns() -> list[URLPattern]:
    """Endpoints bound directly rather than through the router.

    The payment-provider endpoints are singletons -- one per organization, with
    no list and no primary key -- so they are bound to explicit paths instead of
    being given a router prefix that implies a collection.
    """
    from django.urls import path

    from billing.views import DefaultPaymentProviderView, PaymentProviderViewSet

    return [
        path(
            "billing/payment-provider/",
            PaymentProviderViewSet.as_view({"get": "retrieve_provider"}),
            name="payment-provider",
        ),
        path(
            "billing/payment-provider/default/",
            DefaultPaymentProviderView.as_view(),
            name="payment-provider-default",
        ),
    ]


def register_routes(router: SimpleRouter) -> SimpleRouter:
    """Register every shipped viewset on an existing router."""
    for route in get_routes():
        router.register(route["regex"], route["viewset"], basename=route["basename"])
    return router


def billing_router() -> DefaultRouter:
    """A router carrying only this app's endpoints."""
    return register_routes(DefaultRouter())  # type: ignore[return-value]
