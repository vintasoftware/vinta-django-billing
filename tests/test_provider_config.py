"""Provider credentials come from ``VINTA_BILLING['PROVIDERS']``, and the
registries hand back adapters that are actually built.

The extraction left both registries returning adapter *classes*. That reads fine
and passes any test that only asks "is this provider registered?", but every
outbound call site does ``adapter.is_configured`` and then calls a method on it:
on a class, the first is a truthy ``property`` object and the second is an
unbound function. So the checks passed for a provider with no credentials at all,
and the call after them raised ``TypeError``. These tests pin the built shape
down.
"""

import pytest
from django.test import override_settings

from billing.conf import get_provider_config, get_setting, get_site_domain
from billing.constants import PaymentProviders
from billing.services.payment_adapters import (
    get_payment_adapter_classes,
    get_payment_provider_registry,
)
from billing.services.payment_adapters.base import BasePaymentAdapter, select_init_kwargs
from billing.services.payment_service import PaymentService
from billing.services.subscription_adapters import (
    get_subscription_adapter_classes,
    get_subscription_provider_registry,
)
from billing.services.subscription_adapters.base import BaseSubscriptionAdapter


STRIPE_CONFIGURED = {
    "PROVIDERS": {
        "stripe": {
            "API_KEY": "sk_test_configured",
            "WEBHOOK_SECRET": "whsec_test",
            "PUBLISHABLE_KEY": "pk_test",
        }
    }
}


class TestRegistriesReturnBuiltAdapters:
    def test_payment_registry_returns_instances_not_classes(self):
        registry = get_payment_provider_registry()

        for slug, adapter in registry.items():
            assert not isinstance(adapter, type), f"{slug} resolved to a class, not an adapter"
            assert isinstance(adapter, BasePaymentAdapter)

    def test_subscription_registry_returns_instances_not_classes(self):
        registry = get_subscription_provider_registry()

        for slug, adapter in registry.items():
            assert not isinstance(adapter, type), f"{slug} resolved to a class, not an adapter"
            assert isinstance(adapter, BaseSubscriptionAdapter)

    def test_the_class_registries_still_answer_the_registration_question(self):
        """Callers that only need "is this a known slug" do not build anything."""
        assert set(get_payment_adapter_classes()) == set(get_payment_provider_registry())
        assert set(get_subscription_adapter_classes()) == set(get_subscription_provider_registry())
        assert all(isinstance(cls, type) for cls in get_payment_adapter_classes().values())

    def test_is_configured_is_a_real_answer_not_a_truthy_property_object(self):
        """The defect this file exists for: on a class, ``is_configured`` is the
        ``property`` object -- always truthy -- so an unconfigured provider sailed
        through every outbound credential gate."""
        registry = get_payment_provider_registry()

        assert registry[PaymentProviders.STRIPE].is_configured is False


class TestProvidersSetting:
    @override_settings(VINTA_BILLING=STRIPE_CONFIGURED)
    def test_a_configured_provider_reports_itself_configured(self):
        registry = get_payment_provider_registry()

        assert registry[PaymentProviders.STRIPE].is_configured is True
        assert registry[PaymentProviders.STRIPE].api_key == "sk_test_configured"
        assert registry[PaymentProviders.STRIPE].webhook_secret == "whsec_test"

    @override_settings(VINTA_BILLING=STRIPE_CONFIGURED)
    def test_an_unconfigured_provider_is_still_registered(self):
        """Its inbound webhook route has to keep resolving: a deployment that does
        not charge through MercadoPago can still be *sent* a MercadoPago webhook,
        and 500ing on it makes the provider retry forever."""
        registry = get_payment_provider_registry()

        assert PaymentProviders.MERCADOPAGO in registry
        assert registry[PaymentProviders.MERCADOPAGO].is_configured is False

    @override_settings(VINTA_BILLING=STRIPE_CONFIGURED)
    def test_the_outbound_gate_refuses_the_unconfigured_provider(self):
        from billing.exceptions import PaymentProviderNotConfiguredError

        service = PaymentService()

        assert service.get_configured_payment_adapter(PaymentProviders.STRIPE) is not None
        with pytest.raises(PaymentProviderNotConfiguredError):
            service.get_configured_payment_adapter(PaymentProviders.MERCADOPAGO)

    @override_settings(VINTA_BILLING=STRIPE_CONFIGURED)
    def test_the_inbound_path_still_resolves_the_unconfigured_provider(self):
        """The counterpart of the test above -- registry-only resolution must not
        consult credentials at all."""
        service = PaymentService()

        assert service.get_payment_adapter(PaymentProviders.MERCADOPAGO) is not None

    def test_get_provider_config_is_empty_for_an_unconfigured_provider(self):
        assert get_provider_config("stripe") == {}
        assert get_provider_config("not-a-provider") == {}

    @override_settings(VINTA_BILLING=STRIPE_CONFIGURED)
    def test_get_provider_config_copies_so_a_caller_cannot_mutate_settings(self):
        config = get_provider_config("stripe")
        config["API_KEY"] = "tampered"

        assert get_provider_config("stripe")["API_KEY"] == "sk_test_configured"


class TestFromConfig:
    def test_keys_are_lowercased_into_constructor_arguments(self):
        adapter = get_payment_adapter_classes()[PaymentProviders.MERCADOPAGO].from_config(
            {"ACCESS_TOKEN": "token", "WEBHOOK_SECRET": "secret"}
        )

        assert adapter.access_token == "token"
        assert adapter.webhook_secret == "secret"

    def test_keys_the_constructor_does_not_take_are_ignored(self):
        """One provider entry holds everything configured for that provider,
        including the browser-safe key the adapter must never receive."""
        adapter = get_payment_adapter_classes()[PaymentProviders.STRIPE].from_config(
            {"API_KEY": "sk", "PUBLISHABLE_KEY": "pk", "SOMETHING_ELSE": 1}
        )

        assert adapter.api_key == "sk"
        assert not hasattr(adapter, "publishable_key")

    def test_an_empty_entry_builds_an_unconfigured_adapter_rather_than_raising(self):
        adapter = get_payment_adapter_classes()[PaymentProviders.STRIPE].from_config({})

        assert adapter.is_configured is False

    def test_a_constructor_taking_kwargs_opts_out_of_the_filtering(self):
        class Greedy:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        assert select_init_kwargs(Greedy, {"ANYTHING": 1}) == {"anything": 1}


class TestSiteDomain:
    """Both settings are driven through the ``settings`` fixture rather than
    ``override_settings``.

    Mixing the two leaks: an ``override_settings`` decorator on a test that also
    takes the ``settings`` fixture is unwound *after* the fixture restores, so
    ``django.conf.settings`` itself still carries the decorator's value in the
    next test. That is a pytest-django interaction, not something this package
    can fix -- the settings object really is wrong by then -- so the tests here
    just use one mechanism."""

    def test_the_namespaced_setting_wins(self, settings):
        settings.SITE_DOMAIN = "legacy.example.com"
        settings.VINTA_BILLING = {"SITE_DOMAIN": "api.example.com"}

        assert get_site_domain() == "api.example.com"

    def test_it_falls_back_to_a_top_level_setting(self, settings):
        settings.SITE_DOMAIN = "legacy.example.com"
        settings.VINTA_BILLING = {}

        assert get_site_domain() == "legacy.example.com"

    def test_unset_everywhere_is_none(self, settings):
        settings.SITE_DOMAIN = None
        settings.VINTA_BILLING = {}

        assert get_site_domain() is None


class TestDefaultProvider:
    def test_it_defaults_to_stripe(self):
        assert get_setting("DEFAULT_PROVIDER") == PaymentProviders.STRIPE

    @override_settings(VINTA_BILLING={"DEFAULT_PROVIDER": "mercadopago"})
    def test_a_project_can_point_it_elsewhere(self):
        from billing.services.payment_provider_resolver import PaymentProviderResolver

        assert PaymentProviderResolver().resolve_default() == PaymentProviders.MERCADOPAGO
