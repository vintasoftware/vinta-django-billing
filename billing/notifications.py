"""Where dunning and usage-warning messages go.

The engine decides *that* an organization should be told its card failed or that
it is close to a limit. It does not decide how -- that is a transport, and every
project already has one.

Services take a notifier as a constructor argument and fall back to the
configured default, so a test can pass a recorder without touching settings:

    VINTA_BILLING = {'NOTIFIER': 'billing.notifications.VintaSendNotifier'}
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable


logger = logging.getLogger(__name__)


class NotificationTypes:
    """The delivery channels the shipped services ask for.

    Values match ``vintasend.constants.NotificationTypes`` so the adapter below
    is a pass-through, but they are declared here so nothing in the engine
    imports vintasend.
    """

    EMAIL = "EMAIL"
    IN_APP = "IN_APP"
    SMS = "SMS"
    PUSH = "PUSH"


@runtime_checkable
class Notifier(Protocol):
    """One method, matching the shape the services call."""

    def create_notification(
        self,
        user_id: Any,
        notification_type: str,
        title: str,
        body_template: str,
        context_name: str,
        context_kwargs: dict[str, Any],
        subject_template: str | None = None,
        preheader_template: str | None = None,
        send_after: Any = None,
    ) -> Any: ...


class LoggingNotifier:
    """The default: record the intent and drop it.

    A project that has not wired a transport yet gets a log line per
    notification rather than an exception on the dunning path -- a failed
    delivery must never roll back the billing transition that triggered it.
    """

    def create_notification(
        self,
        user_id: Any,
        notification_type: str,
        title: str,
        body_template: str,
        context_name: str,
        context_kwargs: dict[str, Any],
        subject_template: str | None = None,
        preheader_template: str | None = None,
        send_after: Any = None,
    ) -> None:
        logger.info(
            "billing notification not delivered: no notifier configured",
            extra={
                "user_id": user_id,
                "notification_type": notification_type,
                "context_name": context_name,
            },
        )


class VintaSendNotifier:
    """Adapter onto ``vintasend``. Requires the ``vintasend`` extra."""

    def __init__(self, notification_service: Any = None) -> None:
        if notification_service is None:
            # Imported lazily so the module is importable without the extra.
            # `NotificationService` reads its adapters and backend from the
            # project's vintasend settings, so it takes no arguments here.
            from vintasend.services.notification_service import NotificationService

            notification_service = NotificationService()
        self._service = notification_service

    def create_notification(
        self,
        user_id: Any,
        notification_type: str,
        title: str,
        body_template: str,
        context_name: str,
        context_kwargs: dict[str, Any],
        subject_template: str | None = None,
        preheader_template: str | None = None,
        send_after: Any = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "user_id": user_id,
            "notification_type": notification_type,
            "title": title,
            "body_template": body_template,
            "context_name": context_name,
            "context_kwargs": context_kwargs,
        }
        if subject_template is not None:
            kwargs["subject_template"] = subject_template
        if preheader_template is not None:
            kwargs["preheader_template"] = preheader_template
        if send_after is not None:
            kwargs["send_after"] = send_after
        return self._service.create_notification(**kwargs)


def get_notifier() -> Notifier:
    """The configured notifier, instantiated per call.

    Not cached: a notifier can hold a transport connection, and the services
    already keep the instance they were handed for the length of a call.
    """
    from billing.conf import get_object_from_setting

    notifier = get_object_from_setting("NOTIFIER")
    if isinstance(notifier, type):
        return notifier()  # type: ignore[no-any-return]
    return notifier  # type: ignore[no-any-return]
