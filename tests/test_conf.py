"""Settings resolution, and the seams that read it."""

import pytest
from django.test import override_settings

from vinta_billing.conf import get_object_from_setting, get_setting
from vinta_billing.metering import NullOccurrenceSource, get_occurrence_source
from vinta_billing.notifications import LoggingNotifier, get_notifier


class TestGetSetting:
    def test_returns_the_default_when_unconfigured(self):
        with override_settings(VINTA_BILLING={}):
            assert get_setting("DEFAULT_CURRENCY") == "USD"

    def test_an_override_wins(self):
        with override_settings(VINTA_BILLING={"DEFAULT_CURRENCY": "BRL"}):
            assert get_setting("DEFAULT_CURRENCY") == "BRL"

    def test_the_cache_is_dropped_on_override(self):
        """`override_settings` must not leave a stale resolved dictionary behind."""
        with override_settings(VINTA_BILLING={"DEFAULT_CURRENCY": "BRL"}):
            assert get_setting("DEFAULT_CURRENCY") == "BRL"
        with override_settings(VINTA_BILLING={}):
            assert get_setting("DEFAULT_CURRENCY") == "USD"

    def test_an_unknown_key_is_rejected(self):
        """A typo would otherwise be silent -- the default is used and the
        project believes it configured something."""
        with override_settings(VINTA_BILLING={"DEFULT_CURRENCY": "BRL"}):
            with pytest.raises(ValueError, match="Unknown VINTA_BILLING key"):
                get_setting("DEFAULT_CURRENCY")


class TestGetObjectFromSetting:
    def test_imports_a_dotted_path(self):
        assert get_object_from_setting("NOTIFIER") is LoggingNotifier

    def test_returns_none_for_an_unset_optional_hook(self):
        with override_settings(VINTA_BILLING={}):
            assert get_object_from_setting("OCCURRENCE_SOURCE") is None

    def test_passes_a_non_string_through(self):
        """Tests and programmatic configuration hand over the object itself."""
        sentinel = object()
        with override_settings(VINTA_BILLING={"NOTIFIER": sentinel}):
            assert get_object_from_setting("NOTIFIER") is sentinel


class TestDefaults:
    def test_the_default_notifier_drops_messages_without_raising(self, caplog):
        """A failed delivery must never roll back the transition that caused it."""
        notifier = get_notifier()

        assert isinstance(notifier, LoggingNotifier)
        notifier.create_notification(
            user_id=1,
            notification_type="EMAIL",
            title="t",
            body_template="b",
            context_name="c",
            context_kwargs={},
        )

    def test_the_default_occurrence_source_meters_nothing(self):
        """Correct for a project that only caps prepaid resources."""
        with override_settings(VINTA_BILLING={}):
            source = get_occurrence_source()

        assert isinstance(source, NullOccurrenceSource)
        assert list(source.iter_occurrences([1], None, None)) == []
        assert source.describe([1, 2]) == {}
