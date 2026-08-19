"""The two seams that let a project mount the shipped routes as they are.

Both exist because of what a host application had to do without them. It ran
its own copy of the REST layer -- seven viewset subclasses across two modules,
plus a route table -- for two reasons and no others: its DRF surface resolves
the acting organization from a header of its own, and its services are built by
its own ``dependency_injector`` container. Neither is a thing a billing library
should own, and neither was reachable from the outside, so the whole layer had
to be restated to change two lines of it.

``VIEW_MIXIN`` and ``SERVICE_CONTAINER`` are those two lines. Both default to
today's behaviour, which the first test in each class below is about: a project
that configures nothing mounts the same classes, built from the same container,
as it did before either setting existed.
"""

from __future__ import annotations

import importlib
from typing import ClassVar

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import clear_url_caches, reverse
from django.utils.module_loading import import_string
from rest_framework.test import APIClient
from vinta_orgs.conf import get_organization_membership_model, get_organization_model

import tests.urls_seams
from vinta_billing.billing_views import (
    AddOnViewSet,
    BillingPlanViewSet,
    BillingUsageViewSet,
    SubscriptionViewSet,
)
from vinta_billing.routing import get_routes
from vinta_billing.services.container import (
    get_entitlement_service,
    get_service_container,
    resolve_service,
)
from vinta_billing.services.entitlement_service import EntitlementService
from vinta_billing.view_mixins import TenantScopedViewMixin, apply_view_mixin
from vinta_billing.views import PaymentsViewSet


class HeaderScopedViewMixin:
    """A project's own tenant scoping, in the shape they are actually written.

    Modelled on ``vinta_orgs.drf.OrganizationScopedAPIViewMixin`` and the host
    mixin built on it: resolution happens between "``request.user`` is real" and
    "permissions run", it *assigns* ``request.organization`` as a side effect,
    and ``resolve_organization`` returns ``None``.

    That last detail is the one that matters. This mixin goes in front, so its
    ``resolve_organization`` wins on name resolution over the package's, whose
    caller assigns whatever came back -- and assigning that ``None`` would undo
    the resolution and 403 every billing endpoint. The host carried a mixin
    whose entire job was working around that.
    """

    #: Every request this mixin resolved for, so a test can prove it ran.
    resolved: ClassVar[list[str]] = []

    def perform_authentication(self, request):
        super().perform_authentication(request)
        self.resolve_organization(request)

    def resolve_organization(self, request):
        raw_id = request.headers.get("X-Organization-Id")
        HeaderScopedViewMixin.resolved.append(raw_id or "")
        request.organization = (
            get_organization_model().objects.filter(pk=raw_id).first() if raw_id else None
        )
        return None


class RecordingEntitlementService(EntitlementService):
    """Tells the container it was the one that built this."""


class FakeContainer:
    """A stand-in for a project's own DI container.

    Shaped like ``dependency_injector``'s: a provider is an attribute named
    after the service it builds, and calling it builds one. This package's own
    container spells the same thing ``get_<name>()``; both are accepted.
    """

    def __init__(self):
        self.built = []

    def entitlement_service(self):
        service = RecordingEntitlementService()
        self.built.append(service)
        return service


def _mount(view_mixin=None, service_container=None):
    """Rebuild ``tests.urls_seams`` under the given settings, and return the override.

    Used as a context manager. The reload is what makes this a real mounting
    test rather than a test of ``get_routes()``'s return value: the route table
    is built while the settings are in force, exactly as it is when a project's
    urlconf is imported at startup.
    """
    billing_settings = {}
    if view_mixin is not None:
        billing_settings["VIEW_MIXIN"] = view_mixin
    if service_container is not None:
        billing_settings["SERVICE_CONTAINER"] = service_container

    class _Mounted:
        def __enter__(self):
            self.override = override_settings(
                VINTA_BILLING=billing_settings, ROOT_URLCONF="tests.urls_seams"
            )
            self.override.enable()
            importlib.reload(tests.urls_seams)
            clear_url_caches()
            return self

        def __exit__(self, *exc):
            self.override.disable()
            importlib.reload(tests.urls_seams)
            clear_url_caches()
            return False

    return _Mounted()


@pytest.fixture
def logged_in_client(db):
    user = get_user_model().objects.create_user(username="mounted", password="pw")
    client = APIClient()
    client.force_login(user)
    return client


class TestTheViewMixinSeam:
    def test_the_default_mounts_the_very_same_classes(self):
        """Identity, not equality: a project that configures nothing gets the
        classes this package has always exported, not generated copies of
        them. ``VIEW_MIXIN`` names the mixin those viewsets already inherit, so
        there is nothing to mix in."""
        mounted = {route["basename"]: route["viewset"] for route in get_routes()}

        assert mounted["BillingUsage"] is BillingUsageViewSet
        assert mounted["BillingSubscription"] is SubscriptionViewSet
        assert mounted["BillingAddOn"] is AddOnViewSet

    def test_a_configured_mixin_goes_in_front_of_every_tenant_scoped_viewset(self):
        with override_settings(VINTA_BILLING={"VIEW_MIXIN": HeaderScopedViewMixin}):
            mounted = {route["basename"]: route["viewset"] for route in get_routes()}

            for basename in (
                "BillingProfile",
                "BillingUsage",
                "BillingUsagePeriod",
                "BillingUsageOccurrence",
                "BillingSubscription",
                "BillingAddOn",
            ):
                viewset = mounted[basename]
                assert viewset.__mro__[1] is HeaderScopedViewMixin, basename
                assert issubclass(viewset, TenantScopedViewMixin), basename

    def test_a_viewset_that_is_not_tenant_scoped_is_left_alone(self):
        """The plan catalogue answers the same for every caller, and the inbound
        provider webhooks are authenticated by a provider signature rather than
        by a member of anything. Neither takes a project's tenant scoping."""
        with override_settings(VINTA_BILLING={"VIEW_MIXIN": HeaderScopedViewMixin}):
            assert apply_view_mixin(BillingPlanViewSet) is BillingPlanViewSet
            assert apply_view_mixin(PaymentsViewSet) is PaymentsViewSet

    def test_the_same_class_comes_back_every_time(self):
        """A urlconf is walked more than once -- reverse lookups, schema
        generation, a resolver rebuild -- and a fresh class per call would hand
        out several classes wearing one name."""
        with override_settings(VINTA_BILLING={"VIEW_MIXIN": HeaderScopedViewMixin}):
            assert apply_view_mixin(BillingUsageViewSet) is apply_view_mixin(BillingUsageViewSet)

    def test_a_mixin_that_extends_this_packages_own_still_linearizes(self):
        """The other shape a project's mixin takes: a subclass of the one
        shipped here, overriding ``resolve_organization`` alone. It goes in
        front like any other, and the resulting MRO puts the subclass before
        the viewset and the base after both."""

        class NarrowedMixin(TenantScopedViewMixin):
            def resolve_organization(self, request):
                return None

        with override_settings(VINTA_BILLING={"VIEW_MIXIN": NarrowedMixin}):
            built = apply_view_mixin(BillingUsageViewSet)

        assert built.__mro__[1] is NarrowedMixin
        assert built.__mro__.index(BillingUsageViewSet) < built.__mro__.index(TenantScopedViewMixin)

    def test_the_documented_default_imports(self):
        assert import_string("vinta_billing.view_mixins.TenantScopedViewMixin") is (
            TenantScopedViewMixin
        )


class TestTheServiceContainerSeam:
    def test_the_default_is_this_packages_own_container(self):
        assert get_service_container() is importlib.import_module(
            "vinta_billing.services.container"
        )
        assert resolve_service("entitlement_service") is get_entitlement_service()

    def test_a_projects_container_is_asked_instead(self):
        container = FakeContainer()

        with override_settings(VINTA_BILLING={"SERVICE_CONTAINER": container}):
            service = resolve_service("entitlement_service")

        assert isinstance(service, RecordingEntitlementService)
        assert container.built == [service]

    def test_a_dotted_path_to_an_object_inside_a_module_resolves(self):
        """The spelling a project running ``dependency_injector`` uses: the
        container is an instance built in its app's ``ready()``, named as an
        attribute of the module that holds it."""
        tests.test_integration_seams.module_level_container = FakeContainer()  # type: ignore[attr-defined]
        try:
            with override_settings(
                VINTA_BILLING={
                    "SERVICE_CONTAINER": "tests.test_integration_seams.module_level_container"
                }
            ):
                service = resolve_service("entitlement_service")

            assert isinstance(service, RecordingEntitlementService)
        finally:
            del tests.test_integration_seams.module_level_container  # type: ignore[attr-defined]

    def test_a_container_missing_a_service_says_which_one(self):
        from django.core.exceptions import ImproperlyConfigured

        with override_settings(VINTA_BILLING={"SERVICE_CONTAINER": FakeContainer()}):
            with pytest.raises(ImproperlyConfigured, match="payment_service"):
                resolve_service("payment_service")

    def test_an_injected_service_still_wins_over_both(self):
        """The call a project with its own container makes today is unchanged:
        pass the service by keyword and no container is consulted at all."""
        mine = EntitlementService()

        with override_settings(VINTA_BILLING={"SERVICE_CONTAINER": FakeContainer()}):
            view = BillingUsageViewSet(entitlement_service=mine)

        assert view.entitlement_service is mine


class TestBothSeamsThroughAMountedRoute:
    """The bar this release aims at: a project mounts ``get_routes()`` and
    ``get_extra_patterns()`` as they are, and its own scoping and its own
    services are what serve the request."""

    def test_the_shipped_route_uses_the_projects_mixin_and_the_projects_container(
        self, logged_in_client, db
    ):
        organization = get_organization_model().objects.create(name="Mounted", slug="mounted")
        container = FakeContainer()
        HeaderScopedViewMixin.resolved.clear()

        with _mount(view_mixin=HeaderScopedViewMixin, service_container=container):
            response = logged_in_client.get(
                reverse("billing:BillingUsage-retrieve"),
                HTTP_X_ORGANIZATION_ID=str(organization.pk),
            )

        # The project's mixin resolved the organization -- from a header this
        # package has never heard of -- and the view served it rather than
        # answering "an active organization is required".
        assert response.status_code == 200, response.data
        assert HeaderScopedViewMixin.resolved == [str(organization.pk)]
        # And the usage was computed by the service the project's container
        # built, not by one this package cached for itself.
        assert container.built, "the configured container was never asked for a service"
        assert all(isinstance(service, RecordingEntitlementService) for service in container.built)

    def test_vinta_orgs_own_drf_mixin_composes_without_an_adapter(self, db):
        """Not a stand-in this time: ``vinta_orgs.drf
        .OrganizationScopedAPIViewMixin`` itself, which is what a project's own
        tenant-scoped base viewset is built on and where the assign-and-return-
        ``None`` shape comes from. Configured directly, with nothing of this
        package's in between, it resolves the organization for a shipped
        endpoint."""
        organization = get_organization_model().objects.create(name="Composed", slug="composed")
        user = get_user_model().objects.create_user(username="composed", password="pw")
        get_organization_membership_model().objects.create(organization=organization, user=user)
        client = APIClient()
        client.force_login(user)

        with _mount(view_mixin="vinta_orgs.drf.OrganizationScopedAPIViewMixin"):
            response = client.get(
                reverse("billing:BillingUsage-retrieve"),
                HTTP_ORGANIZATION_SLUG=organization.slug,
            )

        assert response.status_code == 200, response.data

    def test_without_the_mixin_the_same_request_resolves_no_organization(
        self, logged_in_client, db
    ):
        """The other half of the previous test: the header means nothing to this
        package on its own, so it is the configured mixin -- not the request --
        that is doing the work above."""
        organization = get_organization_model().objects.create(name="Mounted", slug="mounted")

        with _mount():
            response = logged_in_client.get(
                reverse("billing:BillingUsage-retrieve"),
                HTTP_X_ORGANIZATION_ID=str(organization.pk),
            )

        assert response.status_code == 403


class TestThePackageShipsItsTypes:
    def test_the_py_typed_marker_is_in_the_package(self):
        """Without it every annotation in this package reads as ``Any`` to a
        consumer running mypy -- and a star re-export from an ``Any`` module
        re-exports nothing, so a project re-exporting these models sees an
        `[attr-defined]` error per name. PEP 561 asks for the marker file; the
        wheel and the sdist carry it because hatchling ships everything under
        the package directory."""
        import pathlib

        import vinta_billing

        marker = pathlib.Path(vinta_billing.__file__).parent / "py.typed"

        assert marker.is_file()
