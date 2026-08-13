"""Where dunning and usage-warning messages go.

The engine decides *that* an organization should be told its card failed or that
it is close to a limit, and hands over the facts. It does not decide how the
message is delivered or rendered -- that is a transport, every project already
has one, and this package deliberately ships no adapter for any of them.

A notifier is anything with :meth:`Notifier.create_notification`. Point
``NOTIFIER`` at yours:

    # settings.py
    VINTA_BILLING = {'NOTIFIER': 'myproject.billing.Notifier'}

    # myproject/billing.py
    class Notifier:
        def create_notification(self, user_id, notification_type, title,
                                body_template, context_name, context_kwargs,
                                **kwargs):
            my_transport.send(user_id, title, context_name, context_kwargs)

``context_name`` names the message ("dunning_entered_grace_context",
"approaching_limit_context", ...) and ``context_kwargs`` carries the facts it
needs. Rendering them into a template is the project's job -- shipping the
templates here would mean shipping an opinion about the transport too.

Services take a notifier as a constructor argument and fall back to the
configured one, so a test can pass a recorder without touching settings.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


logger = logging.getLogger(__name__)


class NotificationTypes(StrEnum):
    """The delivery channels the shipped services ask for.

    A hint to the notifier about which channel the engine intends, not a
    promise that the project supports it -- a notifier is free to ignore the
    ones it does not implement.

    A ``StrEnum`` rather than a plain class of string constants, for two
    reasons: the services read ``NotificationTypes.EMAIL.value``, which a bare
    ``str`` attribute does not support; and being a ``str`` subclass means a
    notifier that expects a plain string still receives one.
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


def get_notifier() -> Notifier:
    """The configured notifier, instantiated per call.

    Not cached: a notifier can hold a transport connection, and the services
    already keep the instance they were handed for the length of a call.
    """
    from vinta_billing.conf import get_object_from_setting

    notifier = get_object_from_setting("NOTIFIER")
    if isinstance(notifier, type):
        return notifier()  # type: ignore[no-any-return]
    return notifier  # type: ignore[no-any-return]
