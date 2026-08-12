"""The notifier, the occurrence source, and callback URL building."""

import datetime

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from billing.metering import Occurrence, get_occurrence_source
from billing.notifications import LoggingNotifier, NotificationTypes, get_notifier
from billing.urls_helpers import absolute_url, namespaced


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def create_notification(self, **kwargs):
        self.calls.append(kwargs)


class StubSource:
    def iter_occurrences(self, organization_ids, window_start, window_end):
        return [
            Occurrence(external_id=1, organization_id=organization_ids[0], occurred_at=window_start)
        ]

    def describe(self, external_ids):
        return {external_id: {"title": "n%d" % external_id} for external_id in external_ids}


class TestLoggingNotifierDefault:
    def test_it_accepts_the_full_call_shape_without_raising(self, caplog):
        """The default has to swallow everything the services send it: a failed
        delivery must never roll back the billing transition behind it."""
        LoggingNotifier().create_notification(
            user_id=7,
            notification_type=NotificationTypes.EMAIL,
            title="t",
            body_template="b",
            context_name="dunning_entered_grace_context",
            context_kwargs={"organization_name": "Acme"},
            subject_template="s",
            preheader_template="p",
            send_after=None,
        )

    def test_no_transport_adapter_ships_with_the_package(self):
        """`Notifier` is the whole notification surface. Shipping an adapter for
        one transport would couple this package's releases to that transport's
        API, and the adapter is a dozen lines in the project either way.
        """
        import billing.notifications as notifications

        adapters = [
            name
            for name in dir(notifications)
            if name.endswith("Notifier") and name not in {"Notifier", "LoggingNotifier"}
        ]
        assert adapters == []


class TestGetNotifier:
    def test_instantiates_a_configured_class(self):
        with override_settings(
            VINTA_BILLING={"NOTIFIER": "tests.test_delivery_seams.RecordingNotifier"}
        ):
            assert isinstance(get_notifier(), RecordingNotifier)

    def test_accepts_an_already_built_instance(self):
        """Lets a test hand over a recorder without a dotted path."""
        notifier = RecordingNotifier()

        with override_settings(VINTA_BILLING={"NOTIFIER": notifier}):
            assert get_notifier() is notifier


class TestOccurrenceSource:
    def test_the_configured_source_is_used(self):
        with override_settings(
            VINTA_BILLING={"OCCURRENCE_SOURCE": "tests.test_delivery_seams.StubSource"}
        ):
            source = get_occurrence_source()

        assert source.describe([1, 2]) == {1: {"title": "n1"}, 2: {"title": "n2"}}

    def test_occurrences_carry_a_default_quantity_of_one(self):
        occurrence = Occurrence(
            external_id=1,
            organization_id=2,
            occurred_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        )

        assert occurrence.quantity == 1
        assert occurrence.detail == {}


class TestCallbackUrls:
    def test_prefixes_the_configured_namespace(self):
        assert namespaced("Payments-payment-update") == "billing:Payments-payment-update"

    @override_settings(VINTA_BILLING={"URL_NAMESPACE": ""})
    def test_an_empty_namespace_leaves_the_name_alone(self):
        assert namespaced("Payments-payment-update") == "Payments-payment-update"

    @override_settings(VINTA_BILLING={"SITE_DOMAIN": "https://api.example.com/"})
    def test_builds_an_absolute_url_and_strips_a_trailing_slash(self):
        url = absolute_url("Payments-payment-update", provider="stripe", pk=7)

        assert url.startswith("https://api.example.com/")
        assert "//api.example.com//" not in url

    @override_settings(VINTA_BILLING={})
    def test_refuses_to_build_a_relative_callback_url(self, settings):
        """A provider calls back from outside the process, so a relative path is
        useless -- and fails much later, as an unexplained missing webhook."""
        settings.SITE_DOMAIN = None

        with pytest.raises(ImproperlyConfigured, match="absolute base"):
            absolute_url("Payments-payment-update", provider="stripe", pk=7)


class TestRegistryBackedSerializerFields:
    """The fields exist because a serializer class is built at import time, when
    the registry a project fills from ``AppConfig.ready()`` is still empty."""

    def test_accepts_a_registered_key(self):
        from billing.fields import ResourceKeyField

        assert ResourceKeyField().to_internal_value("widgets") == "widgets"

    def test_rejects_an_unregistered_key(self):
        from rest_framework.exceptions import ValidationError

        from billing.fields import ResourceKeyField

        with pytest.raises(ValidationError):
            ResourceKeyField().to_internal_value("never_registered")

    def test_choices_track_the_registry_rather_than_construction_time(self):
        """The whole point: the field was built before registration ran."""
        from billing.fields import EntitlementKeyField, ResourceKeyField

        assert "widgets" in ResourceKeyField().choices
        assert "white_label" in EntitlementKeyField().choices

    def test_grouped_choices_are_flat(self):
        """DRF renders the browsable-API form from this; the registries have no
        notion of option groups."""
        from billing.fields import ResourceKeyField

        field = ResourceKeyField()

        assert field.grouped_choices == field.choices
