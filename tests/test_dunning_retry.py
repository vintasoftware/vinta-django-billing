"""The two retry paths, and the money-path guard between them.

``tests/test_dunning.py`` drives the ladder against a ``FakeSubscriptionService``
whose ``retry_failed_charge`` is a no-op recorder -- correct for testing the
ladder's *scheduling* (buckets, throttles, expiry), and it means the real method
went untested through 0.5.0. Four properties are documented at length on
``SubscriptionService.retry_failed_charge`` and were enforced nowhere:

* ``CollectionNotSupportedError`` **re-raises** for any provider other than
  MercadoPago, rather than falling back into ``_ensure_provider_plan`` +
  ``change_subscription_plan`` -- the operation a live Stripe probe proved
  collects **$0.00** against a real past-due invoice, with an ``INFO`` log the
  only way to notice. This is the money-path guard, and it is the reason this
  module exists.
* MercadoPago's fallback ladder runs ``pay_outstanding_invoice``,
  ``create_subscription_plan``, ``change_subscription_plan``, in that order,
  against the *subscription's* provider.
* ``NoOutstandingBalanceError`` is swallowed: a beat tick must not raise on a
  state it cannot fix by raising -- a raising job is redelivered and fails
  identically forever.
* A blank ``external_id`` is tolerated on the beat path, deliberately unlike
  ``retry_payment``, which raises ``SubscriptionNotAttachedError`` for the same
  condition because a user is waiting on the answer.

The first four classes below were carried up from the host application that
found the gap, which had been holding this coverage on the package's behalf
(``payments/tests/services/test_dunning_retry_tolerance.py``). Rewritten against
this suite's own fixtures and the swappable organization model, so they hold
under ``tests.settings_swapped`` too.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

import pytest
from django.utils import timezone

from vinta_billing.constants import BillingState, PaymentProviders
from vinta_billing.exceptions import (
    ChargeDeclinedError,
    CollectionNotSupportedError,
    NoOutstandingBalanceError,
    RetryPaymentNotApplicableError,
    SubscriptionNotAttachedError,
)
from vinta_billing.models import PaymentMethod
from vinta_billing.services.dataclasses import CreatedPlan
from vinta_billing.services.dunning_service import (
    DunningService,
    dunning_retry_idempotency_key,
)
from vinta_billing.services.entitlement_service import EntitlementService
from vinta_billing.services.subscription_service import (
    SubscriptionService,
    retry_payment_idempotency_key,
)


pytestmark = pytest.mark.django_db


@dataclass
class FakePaymentService:
    """Precise about *which* provider calls happen and in what order, not about
    their wire shape.

    ``calls`` is the whole point: the money-path guard is a claim about which
    provider methods are reached, so an ordered list of method names is the
    assertion, not a return value.
    """

    plan_external_id: str = "ext-plan-1"
    calls: list[str] = field(default_factory=list)
    create_subscription_plan_providers: list[str] = field(default_factory=list)
    pay_outstanding_invoice_calls: list[tuple[str, str]] = field(default_factory=list)
    update_token_calls: list[str] = field(default_factory=list)
    raise_collection_not_supported: bool = False
    raise_no_outstanding_balance: bool = False
    raise_charge_declined: bool = False

    def create_subscription_plan(self, plan, provider: str) -> CreatedPlan:
        self.calls.append("create_subscription_plan")
        self.create_subscription_plan_providers.append(provider)
        return CreatedPlan(
            id=plan.id,
            name=plan.name,
            value=plan.value,
            currency=plan.currency,
            billing_day=plan.billing_day,
            billing_interval=plan.billing_interval,
            external_id=self.plan_external_id,
        )

    def change_subscription_plan(self, subscription, new_plan, idempotency_key: str = "") -> None:
        self.calls.append("change_subscription_plan")

    def update_subscription_payment_token(self, subscription, payment_token: str) -> None:
        self.calls.append("update_subscription_payment_token")
        self.update_token_calls.append(payment_token)

    def pay_outstanding_invoice(
        self, subscription, payment_token: str = "", idempotency_key: str = ""
    ) -> None:
        self.calls.append("pay_outstanding_invoice")
        self.pay_outstanding_invoice_calls.append((payment_token, idempotency_key))
        if self.raise_collection_not_supported:
            raise CollectionNotSupportedError(
                subscription.pk, "MercadoPago has no verified collection primitive"
            )
        if self.raise_no_outstanding_balance:
            raise NoOutstandingBalanceError(subscription.pk)
        if self.raise_charge_declined:
            raise ChargeDeclinedError(subscription.pk, "card_declined")


@pytest.fixture
def payment_service():
    return FakePaymentService()


@pytest.fixture
def subscription_service(payment_service):
    return SubscriptionService(payment_service=payment_service)


@pytest.fixture
def notifier():
    class Recorder:
        def __init__(self):
            self.calls = []

        def create_notification(self, **kwargs):
            self.calls.append(kwargs)

    return Recorder()


@pytest.fixture
def dunning_service(subscription_service, notifier):
    return DunningService(
        subscription_service=subscription_service,
        entitlement_service=EntitlementService(),
        notification_service=notifier,
    )


@pytest.fixture
def in_grace(subscription):
    """The suite's active subscription moved into a live payment-failure grace
    episode, with an instrument already on file at the provider.

    ``grace_period_ends_at`` is in the future and ``pending_plan`` is unset, so
    ``DunningService._process_grace`` reaches the charge retry rather than
    expiring the window or short-circuiting as a downgrade grace.
    """
    subscription.billing_state = BillingState.GRACE
    subscription.external_id = "already-on-file"
    subscription.payment_provider = PaymentProviders.STRIPE
    subscription.grace_period_ends_at = timezone.now() + datetime.timedelta(days=5)
    subscription.last_dunning_attempt_at = None
    subscription.save(
        update_fields=[
            "billing_state",
            "external_id",
            "payment_provider",
            "grace_period_ends_at",
            "last_dunning_attempt_at",
        ]
    )
    return subscription


class TestTheMoneyPathGuard:
    """``CollectionNotSupportedError`` re-raises for every provider but
    MercadoPago.

    Nothing raises it for Stripe today -- only ``MercadoPagoSubscriptionAdapter``
    does -- so this is the latent case, and latent is exactly why it needs a
    test. If a Stripe subscription ever did raise it, routing it into
    ``_ensure_provider_plan`` + ``change_subscription_plan`` would drive the
    operation that collects $0.00 while flipping the provider-side subscription
    to ``active``: a false recovery, reported as success, with an ``INFO`` log
    as the only trace.
    """

    def test_a_non_mercadopago_provider_reraises_rather_than_falling_back(
        self, subscription_service, payment_service, in_grace
    ):
        payment_service.raise_collection_not_supported = True
        assert in_grace.payment_provider == PaymentProviders.STRIPE

        with pytest.raises(CollectionNotSupportedError):
            subscription_service.retry_failed_charge(in_grace, "dunning-retry-1-0")

        # The fallback never ran: no fresh provider-side plan was minted and no
        # `change_subscription_plan` was driven -- the $0.00 collection the
        # guard exists to keep the ladder away from.
        assert payment_service.calls == ["pay_outstanding_invoice"]

    def test_the_guard_holds_through_the_ladder_that_actually_calls_it(
        self, dunning_service, payment_service, in_grace
    ):
        """The same claim one level up, through the caller that drives this in
        production. ``DunningService._retry_charge_and_notify`` wraps the call in
        ``transaction.atomic()``, so a re-raise unwinds that block -- and
        ``vinta_billing.jobs.process_dunning_for_subscription``'s own
        best-effort guard is what keeps it from poisoning the beat."""
        payment_service.raise_collection_not_supported = True

        with pytest.raises(CollectionNotSupportedError):
            dunning_service.process_subscription(in_grace)

        assert payment_service.calls == ["pay_outstanding_invoice"]

    def test_mercadopago_falls_back_in_the_documented_order(
        self, dunning_service, payment_service, in_grace
    ):
        """MercadoPago's typed refusal is the one case the fallback is for, and
        the fallback keeps that ladder identical in provider calls, arguments and
        idempotency key to what it was before ``pay_outstanding_invoice`` became
        the primary attempt."""
        payment_service.raise_collection_not_supported = True
        in_grace.payment_provider = PaymentProviders.MERCADOPAGO
        in_grace.save(update_fields=["payment_provider"])

        dunning_service.process_subscription(in_grace)

        assert payment_service.calls == [
            "pay_outstanding_invoice",
            "create_subscription_plan",
            "change_subscription_plan",
        ]

    def test_the_fallback_drives_the_subscriptions_own_provider(
        self, subscription_service, payment_service, in_grace, billing_profile
    ):
        """The provider comes off the *subscription*, never the organization's
        current pin. A subscription attached at MercadoPago keeps being driven
        at MercadoPago after the organization repoints its default elsewhere --
        otherwise the fallback would mint a plan at a provider this subscriber
        has no relationship with."""
        payment_service.raise_collection_not_supported = True
        in_grace.payment_provider = PaymentProviders.MERCADOPAGO
        in_grace.save(update_fields=["payment_provider"])
        billing_profile.payment_provider = PaymentProviders.STRIPE
        billing_profile.save(update_fields=["payment_provider"])

        subscription_service.retry_failed_charge(in_grace, "dunning-retry-1-0")

        assert payment_service.create_subscription_plan_providers == [PaymentProviders.MERCADOPAGO]


class TestTheBeatTickToleratesWhatItCannotFix:
    """``retry_failed_charge`` runs from a beat tick under at-least-once
    delivery. Per ``vinta_billing.jobs``' own docstring, a raising job "is
    redelivered and fails identically forever, turning a benign race into a
    permanent stream of alerts" -- so every outcome the tick cannot fix by
    raising is logged and swallowed instead.

    Deliberately unlike ``retry_payment`` (below), which surfaces the analogous
    conditions as 409s: somebody is waiting on that answer.
    """

    def test_a_blank_external_id_returns_unchanged_without_raising(
        self, subscription_service, payment_service, in_grace
    ):
        in_grace.external_id = ""
        in_grace.save(update_fields=["external_id"])

        result = subscription_service.retry_failed_charge(in_grace, "dunning-retry-1-0")

        assert result == in_grace
        # Not even a pointless provider round trip.
        assert payment_service.calls == []

    def test_nothing_owed_returns_unchanged_without_raising(
        self, subscription_service, payment_service, in_grace
    ):
        payment_service.raise_no_outstanding_balance = True

        result = subscription_service.retry_failed_charge(in_grace, "dunning-retry-1-0")

        assert result == in_grace
        # "Nothing owed" is not "this provider cannot collect at all": it must not
        # be mistaken for the MercadoPago refusal and routed into the fallback.
        assert payment_service.calls == ["pay_outstanding_invoice"]

    def test_a_declined_charge_returns_unchanged_without_raising(
        self, subscription_service, payment_service, in_grace
    ):
        """The *common* tick outcome -- a dead card is why the subscription is in
        dunning at all. Left uncaught it would reach the job unhandled and be
        redelivered forever on every subsequent tick."""
        payment_service.raise_charge_declined = True

        result = subscription_service.retry_failed_charge(in_grace, "dunning-retry-1-0")

        assert result == in_grace
        assert payment_service.calls == ["pay_outstanding_invoice"]

    def test_the_ladders_bookkeeping_still_advances_through_a_swallowed_outcome(
        self, dunning_service, payment_service, in_grace, membership
    ):
        """The swallow is not "the tick did not happen". ``last_dunning_attempt_at``
        is stamped and the rung's reminder is sent regardless, which is what walks
        an unresolvable episode to RESTRICTED at grace expiry rather than leaving it
        retrying forever."""
        payment_service.raise_no_outstanding_balance = True

        dunning_service.process_subscription(in_grace)

        in_grace.refresh_from_db()
        assert in_grace.last_dunning_attempt_at is not None

    def test_the_ladders_bucket_key_reaches_the_provider(
        self, subscription_service, payment_service, in_grace
    ):
        """The key is what makes a redelivered tick harmless, so it has to be the
        one the ladder derived -- passed through untouched, and with an empty
        payment token, since the ladder is re-driving the instrument already on
        file and has no new one to attach."""
        key = dunning_retry_idempotency_key(in_grace.pk, 0)

        subscription_service.retry_failed_charge(in_grace, key)

        assert payment_service.pay_outstanding_invoice_calls == [("", key)]


class TestGraceLeavesTheCardAlone:
    """Entering grace must never touch ``PaymentMethod``.

    A failed *charge* says nothing about whether the instrument is still
    attached, and ``has_payment_method`` is what the postpaid guard reads before
    letting an organization keep accruing metered usage. If ``enter_grace``
    deactivated the card, a GRACE organization would stop accruing the moment its
    renewal failed -- silently, and through a path nothing else in the suite
    covers, because every other test sets ``billing_state`` by hand rather than
    going through the transition.
    """

    def test_a_card_on_file_survives_entering_grace(
        self, dunning_service, subscription, organization, membership
    ):
        entitlement_service = EntitlementService()
        PaymentMethod.objects.create(
            organization=organization,
            provider=PaymentProviders.STRIPE,
            external_id="card-on-file",
            is_active=True,
        )
        assert entitlement_service.has_payment_method(organization) is True

        dunning_service.enter_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE
        assert entitlement_service.has_payment_method(organization) is True
        assert PaymentMethod.objects.filter(organization=organization, is_active=True).count() == 1


class TestHasPaymentMethod:
    """The reader itself, which had no test of its own.

    It answers from the ``PaymentMethod`` table rather than inferring from
    ``billing_state`` -- the whole point of that change -- so the cases worth
    pinning are the two that the old state-based proxy got wrong.
    """

    def test_no_record_means_no_instrument(self, organization, entitlement_service):
        assert entitlement_service.has_payment_method(organization) is False

    def test_an_active_record_means_an_instrument(self, organization, entitlement_service):
        PaymentMethod.objects.create(
            organization=organization,
            provider=PaymentProviders.STRIPE,
            external_id="card-1",
            is_active=True,
        )

        assert entitlement_service.has_payment_method(organization) is True

    def test_a_deactivated_record_does_not_count(self, organization, entitlement_service):
        """An admin removing the instrument leaves the row behind, deactivated.
        An ``ACTIVE`` organization with no *current* card is exactly the case the
        old ``billing_state`` proxy answered ``True`` for."""
        PaymentMethod.objects.create(
            organization=organization,
            provider=PaymentProviders.STRIPE,
            external_id="card-1",
            is_active=False,
        )

        assert entitlement_service.has_payment_method(organization) is False

    def test_a_grace_organization_with_a_card_still_has_one(
        self, organization, subscription, entitlement_service
    ):
        """The other case the proxy got wrong, and the one
        ``TestGraceLeavesTheCardAlone`` depends on: ``GRACE`` had to read
        ``False`` categorically under the old inference."""
        subscription.billing_state = BillingState.GRACE
        subscription.save(update_fields=["billing_state"])
        PaymentMethod.objects.create(
            organization=organization,
            provider=PaymentProviders.STRIPE,
            external_id="card-1",
            is_active=True,
        )

        assert entitlement_service.has_payment_method(organization) is True


class TestRetryPaymentOrderingAndKeying:
    """``retry_payment`` -- the user-facing grace-recovery endpoint -- as opposed
    to the ladder's own ``retry_failed_charge`` above.

    Two properties, neither of which the ladder's tests can reach: the new
    instrument is attached **before** the charge is driven, and the key sent to
    the provider is namespaced away from the ladder's.
    """

    def test_the_new_instrument_is_attached_before_the_charge_is_driven(
        self, subscription_service, payment_service, in_grace
    ):
        """Attaching after charging would put the charge on the dead instrument
        one more time -- the exact thing the payer submitted a new card to
        avoid."""
        subscription_service.retry_payment(
            in_grace, payment_token="tok-new-card", idempotency_key="client-1"
        )

        assert payment_service.calls == [
            "update_subscription_payment_token",
            "pay_outstanding_invoice",
        ]
        assert payment_service.update_token_calls == ["tok-new-card"]

    def test_the_charge_carries_the_namespaced_client_key_and_the_new_token(
        self, subscription_service, payment_service, in_grace
    ):
        subscription_service.retry_payment(
            in_grace, payment_token="tok-new-card", idempotency_key="client-1"
        )

        assert payment_service.pay_outstanding_invoice_calls == [
            ("tok-new-card", retry_payment_idempotency_key(in_grace.pk, "client-1"))
        ]

    def test_a_repeat_submission_of_the_same_intent_reuses_the_same_key(
        self, subscription_service, payment_service, in_grace
    ):
        """A double-click, or a client retrying a slow response without
        regenerating its key. The provider collapses the two into one charge
        because the key is byte-identical -- this method deliberately does not
        refuse the second call itself."""
        for _ in range(2):
            subscription_service.retry_payment(
                in_grace, payment_token="tok-new-card", idempotency_key="client-1"
            )

        keys = [key for _token, key in payment_service.pay_outstanding_invoice_calls]
        assert keys == [keys[0], keys[0]]

    def test_a_different_client_key_is_deliberately_allowed_through(
        self, subscription_service, payment_service, in_grace
    ):
        """Indistinguishable from "my first new card was declined, here is
        another one", so it must reach the provider as its own attempt."""
        subscription_service.retry_payment(in_grace, "tok-card-a", "client-1")
        subscription_service.retry_payment(in_grace, "tok-card-b", "client-2")

        keys = [key for _token, key in payment_service.pay_outstanding_invoice_calls]
        assert len(set(keys)) == 2

    def test_the_two_key_namespaces_cannot_collide_on_a_real_pair_of_calls(
        self, subscription_service, payment_service, in_grace
    ):
        """``tests/test_dunning.py`` proves the two *functions* never collide.
        This proves the two *call paths* actually use them: a payer retrying with
        a new card deduplicated against the scheduled attempt that just failed on
        the old card would report success while no money moved."""
        ladder_key = dunning_retry_idempotency_key(in_grace.pk, 0)
        subscription_service.retry_failed_charge(in_grace, ladder_key)
        subscription_service.retry_payment(in_grace, "tok-new-card", "client-1")

        keys = [key for _token, key in payment_service.pay_outstanding_invoice_calls]
        assert keys[0] == ladder_key
        assert keys[1] != ladder_key
        assert len(set(keys)) == 2

    def test_a_blank_external_id_raises_here_rather_than_being_tolerated(
        self, subscription_service, payment_service, in_grace
    ):
        """The deliberate asymmetry with ``retry_failed_charge``: a user-facing
        request must not report success having silently done nothing."""
        in_grace.external_id = ""
        in_grace.save(update_fields=["external_id"])

        with pytest.raises(SubscriptionNotAttachedError):
            subscription_service.retry_payment(in_grace, "tok-new-card", "client-1")

        assert payment_service.calls == []

    def test_a_subscription_that_is_not_in_an_episode_is_refused(
        self, subscription_service, payment_service, in_grace
    ):
        in_grace.billing_state = BillingState.ACTIVE
        in_grace.save(update_fields=["billing_state"])

        with pytest.raises(RetryPaymentNotApplicableError):
            subscription_service.retry_payment(in_grace, "tok-new-card", "client-1")

        assert payment_service.calls == []

    def test_a_downgrade_originated_grace_is_refused_before_anything_is_attached(
        self, subscription_service, payment_service, in_grace, unlimited_plan
    ):
        """There is no failed charge behind a downgrade grace, so there is
        nothing to collect -- and refusing up front is clearer than attaching a
        new instrument for no reason and then bouncing off
        ``NoOutstandingBalanceError``."""
        in_grace.pending_plan = unlimited_plan
        in_grace.save(update_fields=["pending_plan"])

        with pytest.raises(RetryPaymentNotApplicableError):
            subscription_service.retry_payment(in_grace, "tok-new-card", "client-1")

        assert payment_service.calls == []
