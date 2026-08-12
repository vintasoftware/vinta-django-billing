"""Unit tests for ``billing.services.provider_credentials`` and
``billing.services.payment_provider_resolver``.
"""

import pytest
from model_bakery import baker
from organizations.conf import get_organization_model

from billing.constants import PaymentProviders
from billing.exceptions import PaymentProviderNotConfiguredError
from billing.models import BillingProfile
from billing.services.payment_provider_resolver import PaymentProviderResolver
from billing.services.provider_credentials import (
    PublicProviderCredentials,
    resolve_public_credentials,
)


pytestmark = pytest.mark.django_db


class TestResolvePublicCredentials:
    def test_stripe_returns_only_the_stripe_block(self, settings):
        settings.VINTA_BILLING = {
            **settings.VINTA_BILLING,
            "PROVIDERS": {
                "stripe": {"PUBLISHABLE_KEY": "pk_test_stripe"},
                "mercadopago": {"PUBLIC_KEY": "pub_test_mercadopago"},
            },
        }

        credentials = resolve_public_credentials(PaymentProviders.STRIPE)

        assert credentials == PublicProviderCredentials(
            provider=PaymentProviders.STRIPE,
            stripe_publishable_key="pk_test_stripe",
            mercadopago_public_key=None,
        )

    def test_mercadopago_returns_only_the_mercadopago_block(self, settings):
        settings.VINTA_BILLING = {
            **settings.VINTA_BILLING,
            "PROVIDERS": {
                "stripe": {"PUBLISHABLE_KEY": "pk_test_stripe"},
                "mercadopago": {"PUBLIC_KEY": "pub_test_mercadopago"},
            },
        }

        credentials = resolve_public_credentials(PaymentProviders.MERCADOPAGO)

        assert credentials == PublicProviderCredentials(
            provider=PaymentProviders.MERCADOPAGO,
            stripe_publishable_key=None,
            mercadopago_public_key="pub_test_mercadopago",
        )

    def test_raises_when_the_matching_key_is_empty(self, settings):
        settings.VINTA_BILLING = {
            **settings.VINTA_BILLING,
            "PROVIDERS": {"stripe": {"PUBLISHABLE_KEY": ""}},
        }

        with pytest.raises(PaymentProviderNotConfiguredError):
            resolve_public_credentials(PaymentProviders.STRIPE)

    def test_raises_for_an_unknown_provider_slug(self, settings):
        settings.VINTA_BILLING = {
            **settings.VINTA_BILLING,
            "PROVIDERS": {
                "stripe": {"PUBLISHABLE_KEY": "pk_test_stripe"},
                "mercadopago": {"PUBLIC_KEY": "pub_test_mercadopago"},
            },
        }

        with pytest.raises(PaymentProviderNotConfiguredError):
            resolve_public_credentials("not-a-real-provider")


class TestPaymentProviderResolver:
    def test_resolve_for_organization_returns_the_pin_when_set(self):
        organization = baker.make(get_organization_model())
        billing_address = baker.make("billing.BillingAddress")
        baker.make(
            BillingProfile,
            organization=organization,
            contact_email="billing@example.com",
            document_type="CPF",
            document_number="12345678900",
            billing_address=billing_address,
            payment_provider=PaymentProviders.MERCADOPAGO,
        )

        resolver = PaymentProviderResolver()

        assert resolver.resolve_for_organization(organization) == PaymentProviders.MERCADOPAGO

    def test_resolve_for_organization_returns_the_default_when_unpinned(self, settings):
        settings.VINTA_BILLING = {
            **settings.VINTA_BILLING,
            "DEFAULT_PROVIDER": PaymentProviders.STRIPE,
        }
        organization = baker.make(get_organization_model())
        billing_address = baker.make("billing.BillingAddress")
        baker.make(
            BillingProfile,
            organization=organization,
            contact_email="billing@example.com",
            document_type="CPF",
            document_number="12345678900",
            billing_address=billing_address,
            payment_provider="",
        )

        resolver = PaymentProviderResolver()

        assert resolver.resolve_for_organization(organization) == PaymentProviders.STRIPE

    def test_resolve_for_organization_returns_the_default_with_no_billing_profile(self, settings):
        settings.VINTA_BILLING = {
            **settings.VINTA_BILLING,
            "DEFAULT_PROVIDER": PaymentProviders.STRIPE,
        }
        organization = baker.make(get_organization_model())

        resolver = PaymentProviderResolver()

        assert resolver.resolve_for_organization(organization) == PaymentProviders.STRIPE

    def test_resolve_default_reads_the_settings_namespace(self, settings):
        settings.VINTA_BILLING = {
            **settings.VINTA_BILLING,
            "DEFAULT_PROVIDER": PaymentProviders.MERCADOPAGO,
        }

        resolver = PaymentProviderResolver()

        assert resolver.resolve_default() == PaymentProviders.MERCADOPAGO
