"""Signals the engine emits at every billing-significant transition.

The application this was extracted from wrote these straight into its own audit
service. A library cannot assume an audit log exists, so the transitions are
published as signals and a project connects whatever it keeps:

    @receiver(subscription_state_changed)
    def record(sender, subscription, from_state, to_state, actor, **kwargs):
        AuditEntry.objects.create(...)

Every one is sent *after* the change is written and inside the caller's
transaction, so a receiver that raises rolls the transition back with it. A
receiver that must not be able to do that should hand off through
``transaction.on_commit``.
"""

from django.dispatch import Signal


#: A subscription moved between billing states.
#: kwargs: ``subscription``, ``from_state``, ``to_state``, ``actor``, ``reason``
subscription_state_changed = Signal()

#: A subscription was pointed at a different plan.
#: kwargs: ``subscription``, ``from_plan``, ``to_plan``, ``actor``, ``proration``
subscription_plan_changed = Signal()

#: A subscription was cancelled.
#: kwargs: ``subscription``, ``actor``, ``at_period_end``
subscription_cancelled = Signal()

#: A charge reached a terminal status at the provider.
#: kwargs: ``payment``, ``from_status``, ``to_status``
payment_status_changed = Signal()

#: A refund reached a terminal status at the provider.
#: kwargs: ``refund``, ``from_status``, ``to_status``
refund_status_changed = Signal()

#: An organization crossed a warning threshold on a limited resource.
#: kwargs: ``organization``, ``resource_key``, ``level``, ``usage``, ``limit``
limit_warning_raised = Signal()

#: A billing period was closed and its summary written.
#: kwargs: ``subscription``, ``period_start``, ``period_end``, ``summary``
billing_period_closed = Signal()

#: An organization's billing profile was pinned to a different payment provider.
#: kwargs: ``billing_profile``, ``organization``, ``actor``, ``from_provider``,
#: ``to_provider``
payment_provider_repointed = Signal()

#: A subscription left ``restricted``. Projects that paused work while an
#: organization was restricted resume it here.
#: kwargs: ``subscription``, ``organization_ids`` (the pooled subtree)
billing_restriction_lifted = Signal()
