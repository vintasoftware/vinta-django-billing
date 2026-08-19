"""The shipped routes, mounted the way the README says to mount them.

Everything here exists because the suite used to test the viewsets' *methods*
and never the routes. Nothing mounted a router and sent a request, so two
defects shipped unnoticed through a whole release:

* every viewset declared its service as a keyword-only constructor argument with
  no default, which no router can supply -- the URL resolved and the first
  request raised ``TypeError``;
* the two provider webhooks spelled their provider segment as a regex, which a
  path-converter router emits literally.

So these tests dispatch real requests through ``billing_router()``, and resolve
the same URLs again on a converter router.
"""

import pytest
from django.test import override_settings
from django.urls import URLPattern, URLResolver, resolve, reverse

from vinta_billing.routing import billing_router, get_extra_patterns, get_routes
from vinta_billing.services.entitlement_service import EntitlementService
from vinta_billing.services.payment_provider_resolver import PaymentProviderResolver
from vinta_billing.views import DefaultPaymentProviderView, PaymentProviderViewSet, PaymentsViewSet


#: Every endpoint the package publishes, as ``(url name, reverse kwargs, HTTP
#: method)``. Written out by hand rather than derived from the router, so a route
#: that silently stops being generated fails here instead of shrinking the list.
SHIPPED_ENDPOINTS = [
    ("billing:Payments-payment-update", {"pk": 1, "provider": "stripe"}, "post"),
    (
        "billing:Payments-subscription-payment-update",
        {"pk": 1, "provider": "stripe"},
        "post",
    ),
    ("billing:BillingProfile-retrieve", {}, "get"),
    ("billing:BillingProfile-create", {}, "post"),
    ("billing:BillingProfile-update", {}, "put"),
    ("billing:BillingProfile-partial_update", {}, "patch"),
    ("billing:BillingPlan-list", {}, "get"),
    ("billing:BillingUsage-retrieve", {}, "get"),
    ("billing:BillingUsagePeriod-list", {}, "get"),
    ("billing:BillingUsagePeriod-detail", {"pk": 1}, "get"),
    ("billing:BillingUsageOccurrence-list", {}, "get"),
    ("billing:BillingSubscription-retrieve", {}, "get"),
    ("billing:BillingSubscription-change-plan", {}, "post"),
    ("billing:BillingSubscription-cancel", {}, "post"),
    ("billing:BillingSubscription-retry-payment", {}, "post"),
    ("billing:BillingAddOn-list", {}, "get"),
    ("billing:BillingAddOn-detail", {"pk": 1}, "get"),
    ("billing:payment-provider", {}, "get"),
    ("billing:payment-provider-default", {}, "get"),
]


def _flatten(patterns, prefix=""):
    """Every ``(pattern string, name)`` pair reachable from ``patterns``."""
    for entry in patterns:
        if isinstance(entry, URLResolver):
            yield from _flatten(entry.url_patterns, prefix + str(entry.pattern))
        elif isinstance(entry, URLPattern):
            yield prefix + str(entry.pattern), entry.name


class TestTheDocumentedMounting:
    """``tests/urls.py`` is the README's snippet, so the whole suite runs against
    the mounting a project is told to use."""

    @pytest.mark.parametrize(
        "url_name,kwargs,method",
        SHIPPED_ENDPOINTS,
        ids=[name for name, _, _ in SHIPPED_ENDPOINTS],
    )
    def test_every_shipped_endpoint_reverses(self, url_name, kwargs, method):
        assert reverse(url_name, kwargs=kwargs).startswith("/api/")

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        "url_name,kwargs,method",
        SHIPPED_ENDPOINTS,
        ids=[name for name, _, _ in SHIPPED_ENDPOINTS],
    )
    def test_every_shipped_endpoint_serves_a_request(self, client, url_name, kwargs, method):
        """The defect this catches: a view whose service is a required
        keyword-only argument cannot be built by ``as_view()``, so the request
        died in ``cls(**initkwargs)`` -- before authentication, before
        permissions, before any handler. Every one of these was unreachable.

        What each endpoint *answers* is not the point (an anonymous caller is
        refused by most of them, and a webhook with no signature by the rest);
        the point is that it answers at all rather than raising.
        """
        response = getattr(client, method)(reverse(url_name, kwargs=kwargs))

        assert response.status_code != 500

    def test_the_webhooks_resolve_under_billing_and_keep_their_reverse_names(self):
        """Both webhooks live under ``billing/`` now, alongside every other route
        this package serves, rather than at a bare ``payments/`` prefix. The
        reverse names are what a caller -- including both shipped MercadoPago
        adapters -- actually depends on, and those are pinned here unchanged."""
        assert (
            reverse("billing:Payments-payment-update", kwargs={"pk": 7, "provider": "stripe"})
            == "/api/billing/payments/7/payment-update/stripe/"
        )
        assert (
            reverse(
                "billing:Payments-subscription-payment-update",
                kwargs={"pk": 7, "provider": "mercadopago"},
            )
            == "/api/billing/payments/7/subscription-payment-update/mercadopago/"
        )


class TestServicesDefaultThroughTheContainer:
    def test_every_shipped_viewset_builds_with_no_arguments(self):
        """What ``as_view()`` does: ``cls(**initkwargs)``, with no way to pass a
        service."""
        for route in get_routes():
            route["viewset"]()

        PaymentsViewSet()
        PaymentProviderViewSet()
        DefaultPaymentProviderView()

    def test_the_defaulted_services_are_the_containers(self):
        from vinta_billing.services.container import (
            get_dunning_service,
            get_payment_provider_resolver,
            get_payment_service,
            get_subscription_service,
        )

        view = PaymentsViewSet()

        assert view.payment_service is get_payment_service()
        assert view.subscription_service is get_subscription_service()
        assert view.dunning_service is get_dunning_service()
        assert PaymentProviderViewSet().payment_provider_resolver is (
            get_payment_provider_resolver()
        )

    def test_an_injected_service_still_wins(self):
        """The signature a project with its own DI container calls is unchanged:
        it keeps passing every service by keyword and never reaches a factory."""
        from vinta_billing.billing_views import BillingUsageViewSet

        mine = EntitlementService()
        my_resolver = PaymentProviderResolver()

        assert BillingUsageViewSet(entitlement_service=mine).entitlement_service is mine
        assert (
            PaymentProviderViewSet(payment_provider_resolver=my_resolver).payment_provider_resolver
            is my_resolver
        )
        assert (
            DefaultPaymentProviderView(
                payment_provider_resolver=my_resolver
            ).payment_provider_resolver
            is my_resolver
        )


class TestRouterMode:
    """A project's router mode is its own decision, and mounting this package
    must not overturn it."""

    def test_no_shipped_action_spells_its_url_path_as_a_regex(self):
        """The guard that keeps the defect from coming back. A ``url_path``
        holding ``(?P<...>)`` renders literally on a converter router, and a
        ``url_path`` holding ``<str:...>`` renders literally on a regex one --
        so a router-mounted action may parameterise nothing at all. The provider
        webhooks, which must, are bound by ``get_extra_patterns`` instead."""
        for route in get_routes():
            for action in route["viewset"].get_extra_actions():
                assert "(?P<" not in action.url_path, (
                    f"{route['viewset'].__name__}.{action.__name__} carries a regex "
                    "url_path, which only a regex router renders"
                )
                assert "<" not in action.url_path, (
                    f"{route['viewset'].__name__}.{action.__name__} carries a path-converter "
                    "url_path, which only a converter router renders"
                )

    @override_settings(ROOT_URLCONF="tests.urls_converter")
    def test_a_converter_router_produces_the_same_urls(self):
        assert reverse("billing:BillingAddOn-detail", kwargs={"pk": 3}) == "/api/billing/add-ons/3/"
        assert (
            reverse("billing:Payments-payment-update", kwargs={"pk": 7, "provider": "stripe"})
            == "/api/billing/payments/7/payment-update/stripe/"
        )

    def test_a_converter_router_leaves_no_unrendered_regex_behind(self):
        """Everything the router itself emits is in converter form -- no ``url_path``
        of ours smuggles a regex through it. The two ``re_path`` webhook patterns
        are not the router's and are checked by ``resolve`` below instead."""
        for pattern, name in _flatten(billing_router(use_regex_path=False).urls):
            assert "(?P<" not in pattern, f"{name} kept an unrendered regex: {pattern}"

    @override_settings(ROOT_URLCONF="tests.urls_converter")
    def test_the_webhook_still_resolves_to_its_own_action_on_a_converter_router(self):
        match = resolve("/api/billing/payments/7/payment-update/stripe/")

        assert match.url_name == "Payments-payment-update"
        assert match.kwargs == {"pk": "7", "provider": "stripe"}

    @pytest.mark.django_db
    @override_settings(ROOT_URLCONF="tests.urls_converter")
    def test_a_request_reaches_a_webhook_on_a_converter_router(self, client):
        response = client.post("/api/billing/payments/7/payment-update/stripe/")

        assert response.status_code != 404
        assert response.status_code != 500


class TestExtraPatterns:
    def test_the_trailing_slash_can_follow_the_routers(self):
        """A project running ``DefaultRouter(trailing_slash=False)`` used to get
        slash-less webhook URLs, because they came out of that router. They no
        longer do, so the choice is passed in instead."""
        patterns = get_extra_patterns(trailing_slash=False)

        assert str(patterns[0].pattern).endswith("(?P<provider>[^/.]+)$")
        assert str(patterns[2].pattern) == "billing/payment-provider"

    def test_the_legacy_routes_module_is_the_same_table(self):
        """``vinta_billing.routes`` used to hold a second, drifted copy."""
        from vinta_billing import routes

        assert [route["basename"] for route in routes.routes] == [
            route["basename"] for route in get_routes()
        ]
        assert [pattern.name for pattern in routes.extra_patterns] == [
            pattern.name for pattern in get_extra_patterns()
        ]
