"""The subscription signals: sent on commit, once per change, never for a no-op.

``subscription_state_changed`` (and ``subscription_cancelled``) are sent from
``transition_billing_state``, the one place ``billing_state`` is written, so the
tests drive the real transitions -- grace, restriction, plan-change
confirmation, cancellation through either service -- rather than the function
alone. ``subscription_plan_changed`` is sent where ``Subscription.plan`` moves.
Projects pin the names and kwargs of these signals, so the kwargs are asserted
in full.
"""

import datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from vinta_billing.constants import (
    BillingInterval,
    BillingState,
    BillingStateChangeReason,
    LimitKind,
    PaymentProviders,
    SubscriptionStatuses,
)
from vinta_billing.models import BillingPlan, PlanLimit, Subscription
from vinta_billing.services.billing_state_machine import transition_billing_state
from vinta_billing.services.dataclasses import CreatedPlan
from vinta_billing.services.dunning_service import DunningService
from vinta_billing.services.payment_service import PaymentService
from vinta_billing.services.subscription_service import SubscriptionService
from vinta_billing.signals import (
    subscription_cancelled,
    subscription_plan_changed,
    subscription_state_changed,
)


pytestmark = pytest.mark.django_db

MP = PaymentProviders.MERCADOPAGO


def _record(signal):
    received: list[dict] = []

    def receiver(sender, **kwargs):
        kwargs.pop("signal")
        received.append(kwargs)

    signal.connect(receiver)
    return received, receiver


@pytest.fixture
def state_changes():
    received, receiver = _record(subscription_state_changed)
    yield received
    subscription_state_changed.disconnect(receiver)


@pytest.fixture
def cancellations():
    received, receiver = _record(subscription_cancelled)
    yield received
    subscription_cancelled.disconnect(receiver)


@pytest.fixture
def plan_changes():
    received, receiver = _record(subscription_plan_changed)
    yield received
    subscription_plan_changed.disconnect(receiver)


@pytest.fixture
def staff_user(db):
    return get_user_model().objects.create_user(username="grace", password="pw")


def make_plan(slug, monthly_price):
    plan = BillingPlan.objects.create(
        name=slug.title(),
        slug=slug,
        monthly_price=Decimal(monthly_price),
        annual_price=Decimal(monthly_price) * 10,
        currency="BRL",
        is_active=True,
    )
    for key, kind, price in (
        ("widgets", LimitKind.PREPAID, None),
        ("seats", LimitKind.PREPAID, None),
        ("event_occurrences", LimitKind.POSTPAID, Decimal("0.10")),
    ):
        PlanLimit.objects.create(
            plan=plan, resource_key=key, limit_value=10, kind=kind, overage_unit_price=price
        )
    return plan


class FakePaymentService:
    """Records provider-side calls; does nothing at a provider."""

    def __init__(self):
        self.calls: list[str] = []

    def create_subscription_plan(self, plan, provider):
        self.calls.append("create_subscription_plan")
        return CreatedPlan(
            id=plan.id,
            name=plan.name,
            value=plan.value,
            currency=plan.currency,
            billing_day=plan.billing_day,
            billing_interval=plan.billing_interval,
            external_id="ext-plan-1",
        )

    def change_subscription_plan(self, subscription, new_plan, idempotency_key=""):
        self.calls.append("change_subscription_plan")

    def cancel_subscription(self, subscription):
        self.calls.append("cancel_subscription")


# --- transition_billing_state ---


class TestTransitionBillingState:
    def test_sends_once_on_commit_with_actor_and_reason(
        self, subscription, staff_user, state_changes, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            transition_billing_state(
                subscription,
                BillingState.GRACE,
                actor=staff_user,
                reason=BillingStateChangeReason.PAYMENT_FAILED,
            )
        assert state_changes == []

        for callback in callbacks:
            callback()
        assert state_changes == [
            {
                "subscription": subscription,
                "from_state": BillingState.ACTIVE,
                "to_state": BillingState.GRACE,
                "actor": staff_user,
                "reason": BillingStateChangeReason.PAYMENT_FAILED,
            }
        ]

    def test_a_same_state_call_sends_nothing(
        self, subscription, state_changes, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            transition_billing_state(subscription, BillingState.ACTIVE)

        assert state_changes == []

    def test_existing_positional_calls_still_work(
        self, subscription, state_changes, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            transition_billing_state(subscription, BillingState.GRACE)

        assert state_changes[0]["actor"] is None
        assert state_changes[0]["reason"] == BillingStateChangeReason.UNSPECIFIED


# --- Dunning ---


class TestDunningTransitions:
    @pytest.fixture
    def dunning(self):
        return DunningService(subscription_service=Mock(), notification_service=Mock())

    def test_enter_grace(
        self, dunning, subscription, state_changes, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            dunning.enter_grace(subscription)
            dunning.enter_grace(subscription)

        assert [(c["from_state"], c["to_state"], c["reason"]) for c in state_changes] == [
            (BillingState.ACTIVE, BillingState.GRACE, BillingStateChangeReason.PAYMENT_FAILED)
        ]

    def test_grace_expiring_into_restricted(
        self, dunning, subscription, state_changes, django_capture_on_commit_callbacks
    ):
        subscription.billing_state = BillingState.GRACE
        subscription.grace_period_ends_at = timezone.now() - datetime.timedelta(days=1)
        subscription.save()
        dunning.check_free_fallback = Mock(return_value=False)

        with django_capture_on_commit_callbacks(execute=True):
            dunning.expire_grace(subscription)

        assert [(c["from_state"], c["to_state"], c["reason"]) for c in state_changes] == [
            (BillingState.GRACE, BillingState.RESTRICTED, BillingStateChangeReason.GRACE_EXPIRED)
        ]

    def test_recovery(
        self, dunning, subscription, state_changes, django_capture_on_commit_callbacks
    ):
        subscription.billing_state = BillingState.GRACE
        subscription.save()

        with django_capture_on_commit_callbacks(execute=True):
            dunning.resolve_payment_success(subscription)

        assert [(c["to_state"], c["reason"]) for c in state_changes] == [
            (BillingState.ACTIVE, BillingStateChangeReason.PAYMENT_SUCCEEDED)
        ]


# --- Plan changes ---


class TestPlanChanges:
    def test_confirm_plan_change_from_free(
        self, subscription, state_changes, plan_changes, django_capture_on_commit_callbacks
    ):
        subscription.billing_state = BillingState.FREE
        subscription.save()

        with django_capture_on_commit_callbacks(execute=True):
            SubscriptionService(payment_service=FakePaymentService()).confirm_plan_change(
                subscription
            )

        assert [(c["from_state"], c["to_state"], c["reason"]) for c in state_changes] == [
            (BillingState.FREE, BillingState.ACTIVE, BillingStateChangeReason.PLAN_CHANGE_CONFIRMED)
        ]
        # The plan did not move -- confirming re-copies the current plan's limits.
        assert plan_changes == []

    def test_change_plan_announces_a_different_plan(
        self, subscription, plan, staff_user, plan_changes, django_capture_on_commit_callbacks
    ):
        other_plan = make_plan("bigger", "50.00")

        with django_capture_on_commit_callbacks(execute=True):
            SubscriptionService().change_plan(subscription, other_plan, actor=staff_user)

        assert plan_changes == [
            {
                "subscription": subscription,
                "from_plan": plan,
                "to_plan": other_plan,
                "actor": staff_user,
                "proration": False,
            }
        ]

    def test_an_upgrade_announces_the_plan_with_proration(
        self, subscription, plan, staff_user, plan_changes, django_capture_on_commit_callbacks
    ):
        subscription.external_id = "sub-ext-1"
        subscription.payment_provider = MP
        subscription.save()
        bigger = make_plan("bigger", "50.00")

        with django_capture_on_commit_callbacks(execute=True):
            SubscriptionService(payment_service=FakePaymentService()).request_plan_change(
                subscription,
                bigger,
                BillingInterval.MONTHLY,
                idempotency_key="change-1",
                actor=staff_user,
            )

        assert [
            (c["from_plan"], c["to_plan"], c["actor"], c["proration"]) for c in plan_changes
        ] == [(plan, bigger, staff_user, True)]

    def test_a_downgrade_enters_grace_with_its_reason(
        self,
        subscription,
        staff_user,
        state_changes,
        plan_changes,
        django_capture_on_commit_callbacks,
    ):
        cheaper = make_plan("cheaper", "1.00")

        with django_capture_on_commit_callbacks(execute=True):
            SubscriptionService(payment_service=FakePaymentService()).request_plan_change(
                subscription,
                cheaper,
                BillingInterval.MONTHLY,
                idempotency_key="change-1",
                actor=staff_user,
            )

        assert [(c["to_state"], c["actor"], c["reason"]) for c in state_changes] == [
            (BillingState.GRACE, staff_user, BillingStateChangeReason.DOWNGRADE_SCHEDULED)
        ]
        # A downgrade waits for the period boundary to move the plan.
        assert plan_changes == []


# --- Cancellation ---


class TestCancellation:
    def test_subscription_service_cancel_sends_each_signal_once(
        self,
        subscription,
        staff_user,
        state_changes,
        cancellations,
        django_capture_on_commit_callbacks,
    ):
        """The provider-side half routes through the same transition, which is a
        no-op by then -- so one cancellation, one of each signal."""
        subscription.external_id = "sub-ext-1"
        subscription.save()
        payment_service = PaymentService(
            subscription_provider_registry={MP: Mock(is_configured=True)}
        )
        payment_service._serialize_subscription = Mock()
        subscription.payment_provider = MP
        subscription.save()

        with django_capture_on_commit_callbacks(execute=True):
            SubscriptionService(payment_service=payment_service).cancel_subscription(
                subscription, actor=staff_user
            )

        assert state_changes == [
            {
                "subscription": subscription,
                "from_state": BillingState.ACTIVE,
                "to_state": BillingState.CANCELLED,
                "actor": staff_user,
                "reason": BillingStateChangeReason.CANCELLED,
            }
        ]
        assert cancellations == [
            {"subscription": subscription, "actor": staff_user, "at_period_end": True}
        ]

    def test_a_failed_provider_cancel_sends_nothing(
        self, subscription, state_changes, cancellations, django_capture_on_commit_callbacks
    ):
        subscription.external_id = "sub-ext-1"
        subscription.save()
        payment_service = Mock()
        payment_service.cancel_subscription.side_effect = RuntimeError("provider down")

        with django_capture_on_commit_callbacks(execute=True):
            with pytest.raises(RuntimeError):
                SubscriptionService(payment_service=payment_service).cancel_subscription(
                    subscription
                )

        assert state_changes == []
        assert cancellations == []
        assert Subscription.objects.get(pk=subscription.pk).billing_state == BillingState.ACTIVE

    def test_payment_service_cancel_moves_billing_state(
        self, subscription, state_changes, cancellations, django_capture_on_commit_callbacks
    ):
        """Before 0.9 this left ``billing_state`` on ``ACTIVE``."""
        adapter = Mock(is_configured=True)
        subscription.payment_provider = MP
        subscription.save()
        payment_service = PaymentService(subscription_provider_registry={MP: adapter})
        payment_service._serialize_subscription = Mock()

        with django_capture_on_commit_callbacks(execute=True):
            payment_service.cancel_subscription(subscription)

        adapter.cancel_subscription.assert_called_once()
        stored = Subscription.objects.get(pk=subscription.pk)
        assert stored.status == SubscriptionStatuses.CANCELLED
        assert stored.billing_state == BillingState.CANCELLED
        assert [(c["from_state"], c["to_state"]) for c in state_changes] == [
            (BillingState.ACTIVE, BillingState.CANCELLED)
        ]
        assert len(cancellations) == 1

    def test_dunning_cancel(
        self, subscription, staff_user, cancellations, django_capture_on_commit_callbacks
    ):
        dunning = DunningService(subscription_service=Mock(), notification_service=Mock())

        with django_capture_on_commit_callbacks(execute=True):
            dunning.cancel(subscription, actor=staff_user)

        assert cancellations == [
            {"subscription": subscription, "actor": staff_user, "at_period_end": True}
        ]
