"""The Django admin's one service-taking call site.

Nothing else in the package builds a service outside a view, and this is the
call site 0.4.0's mountability fix did not reach: Django calls
``save_model(request, obj, form, change)``, and the audited-repoint branch
demanded a fifth argument nothing ever passed.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.test import override_settings

from vinta_billing.admin import BillingProfileAdmin
from vinta_billing.constants import PaymentProviders
from vinta_billing.models import BillingProfile
from vinta_billing.services.subscription_service import SubscriptionService


class TestTheAdminBuildsItsServiceToo:
    """``BillingProfileAdmin.save_model`` was the one 0.4.0 call site left
    without a fallback.

    Django calls ``save_model(request, obj, form, change)``. The audited-repoint
    branch took a fifth ``subscription_service`` argument that nothing in this
    package ever passed, and raised ``RuntimeError`` when it was missing -- so
    every provider repoint made through the admin failed, in the one place a
    staff member is meant to be able to make one.
    """

    @pytest.fixture
    def repoint_form(self):
        class Form:
            changed_data: ClassVar[list[str]] = ["payment_provider"]

        return Form()

    def test_a_repoint_resolves_its_service_and_goes_through(
        self, db, billing_profile, repoint_form, rf
    ):
        request = rf.post("/admin/")
        request.user = get_user_model().objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        billing_profile.payment_provider = PaymentProviders.STRIPE
        model_admin = BillingProfileAdmin(BillingProfile, django_admin.site)

        model_admin.save_model(request, billing_profile, repoint_form, change=True)

        billing_profile.refresh_from_db()
        assert billing_profile.payment_provider == PaymentProviders.STRIPE

    def test_the_repoint_is_built_by_the_configured_container(
        self, db, billing_profile, repoint_form, rf
    ):
        """And it is the *project's* service that runs it, so the audit entry
        lands wherever that project's ``SubscriptionService`` sends it."""
        repoints = []

        class RecordingSubscriptionService(SubscriptionService):
            def set_payment_provider(self, organization, provider, actor=None):
                repoints.append((organization, provider, actor))
                return billing_profile

        class Container:
            def subscription_service(self):
                return RecordingSubscriptionService()

        request = rf.post("/admin/")
        request.user = get_user_model().objects.create_user(
            username="staff2", password="pw", is_staff=True
        )
        billing_profile.payment_provider = PaymentProviders.MERCADOPAGO
        model_admin = BillingProfileAdmin(BillingProfile, django_admin.site)

        with override_settings(VINTA_BILLING={"SERVICE_CONTAINER": Container()}):
            model_admin.save_model(request, billing_profile, repoint_form, change=True)

        assert repoints == [
            (billing_profile.organization, PaymentProviders.MERCADOPAGO, request.user)
        ]

    def test_an_edit_that_does_not_touch_the_provider_asks_for_no_service(
        self, db, billing_profile, rf
    ):
        class Form:
            changed_data: ClassVar[list[str]] = ["contact_email"]

        class ExplodingContainer:
            def __getattr__(self, name):
                raise AssertionError(f"the container was asked for {name}")

        request = rf.post("/admin/")
        request.user = get_user_model().objects.create_user(
            username="staff3", password="pw", is_staff=True
        )
        model_admin = BillingProfileAdmin(BillingProfile, django_admin.site)

        with override_settings(VINTA_BILLING={"SERVICE_CONTAINER": ExplodingContainer()}):
            model_admin.save_model(request, billing_profile, Form(), change=True)
