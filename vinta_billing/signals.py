"""Signals the engine emits at billing-significant transitions.

The application this was extracted from wrote these straight into its own audit
service. A library cannot assume an audit log exists, so the transitions are
published as signals and a project connects whatever it keeps:

    @receiver(subscription_state_changed)
    def record(sender, subscription, from_state, to_state, actor, **kwargs):
        AuditEntry.objects.create(...)

**When each one is sent.** Most are sent through ``transaction.on_commit`` (see
:func:`send_on_commit`): only once the change is durable, never for a change a
rollback undid, and exactly once per write that actually changed the value -- a
same-value write (a webhook redelivery, an idempotent retry) sends nothing. A
receiver that raises is logged by Django and does not affect the caller, the
committed change, or the other receivers; nothing retries it, so a receiver that
must not miss a delivery should hand the work to a durable queue rather than do
it inline.

The exception is ``payment_provider_repointed``, which is still sent inside the
caller's transaction, so a receiver that writes an audit row commits with the
repoint or rolls back with it. Each signal's comment below says which it is.
"""

import functools

from django.db import transaction
from django.dispatch import Signal


#: A subscription moved between billing states. Sent on commit by
#: ``vinta_billing.services.billing_state_machine.transition_billing_state``, the
#: one place ``Subscription.billing_state`` is written, so every transition --
#: grace, restriction, recovery, plan-change confirmation, cancellation, the
#: cycle-close revert to free -- sends it exactly once.
#: kwargs: ``subscription``, ``from_state``, ``to_state``, ``actor``, ``reason``
#: (``reason`` is a ``vinta_billing.constants.BillingStateChangeReason`` value).
subscription_state_changed = Signal()

#: ``Subscription.plan`` was pointed at a different plan. Sent on commit when the
#: column actually changes: when an upgrade is initiated (capacity follows later,
#: when its charge is confirmed) and when a scheduled downgrade takes over at the
#: period boundary. ``proration`` says whether the provider was asked to prorate.
#: kwargs: ``subscription``, ``from_plan``, ``to_plan``, ``actor``, ``proration``
subscription_plan_changed = Signal()

#: A subscription was cancelled: its ``billing_state`` moved to ``CANCELLED``.
#: Sent on commit, alongside ``subscription_state_changed``. ``at_period_end`` is
#: ``True`` because a cancelled subscription keeps running until its paid cycle
#: ends, when the cycle-close sweep reverts it to ``FREE``.
#: kwargs: ``subscription``, ``actor``, ``at_period_end``
subscription_cancelled = Signal()

#: A charge's ``Payment.status`` changed. Sent on commit for **every** change,
#: not only terminal ones (``pending -> in_process`` included); a receiver that
#: only cares about outcomes filters on ``to_status``. Never sent for a status
#: the rules in ``vinta_billing.services.payment_status_machine`` refused, so a
#: terminal status is never followed by a stale one.
#: kwargs: ``payment``, ``from_status``, ``to_status``
payment_status_changed = Signal()

#: A refund's ``Refund.status`` changed. Sent on commit for every change, from
#: the create-refund response and from later ``check_refund_status`` polls.
#: kwargs: ``refund``, ``from_status``, ``to_status``
refund_status_changed = Signal()

#: An scope crossed a warning threshold on a limited resource. Declared, not yet
#: sent by the engine.
#: kwargs: ``scope``, ``resource_key``, ``level``, ``usage``, ``limit``
limit_warning_raised = Signal()

#: A billing period was closed and its summary written. Declared, not yet sent
#: by the engine.
#: kwargs: ``subscription``, ``period_start``, ``period_end``, ``summary``
billing_period_closed = Signal()

#: An scope's billing profile was pinned to a different payment provider. Sent
#: inside the caller's transaction, not on commit.
#: kwargs: ``billing_profile``, ``scope``, ``actor``, ``from_provider``,
#: ``to_provider``
payment_provider_repointed = Signal()

#: A subscription left ``restricted``. Projects that paused work while an
#: scope was restricted resume it here. Sent on commit.
#: kwargs: ``subscription``, ``scope_ids`` (the pooled subtree)
billing_restriction_lifted = Signal()


def send_on_commit(signal: Signal, sender: type, **kwargs: object) -> None:
    """Send ``signal`` once the current transaction commits.

    Runs immediately outside a transaction, and never if the transaction rolls
    back -- Django discards the callback with it. ``send_robust`` rather than
    ``send``: by commit time the change is durable, so one receiver raising must
    not keep the others from hearing about it. Django logs the exception.

    The kwargs are bound now, not when the callback runs, so a caller that goes
    on to change the instance cannot change what the receivers are told the
    transition was.
    """
    transaction.on_commit(functools.partial(signal.send_robust, sender=sender, **kwargs))
