from vinta_billing.constants import BillingInterval
from vinta_billing.models import Subscription
from vinta_billing.services.dataclasses import CreatedPlan
from vinta_billing.services.subscription_plan_factory.base import BaseSubscriptionPlanFactory


class BillingPlanFactory(BaseSubscriptionPlanFactory):
    """Builds the payment-gateway-facing ``CreatedPlan`` dataclass from a
    ``Subscription``'s catalog ``BillingPlan``.

    The plan a subscriber is charged against is the catalog row, read straight
    off ``subscription.plan`` -- there is no per-organization plan model to
    resolve through first.
    """

    def make_plan_from_subscription(self, subscription: Subscription) -> CreatedPlan:
        plan = subscription.plan
        value = (
            plan.annual_price
            if subscription.billing_interval == BillingInterval.ANNUAL
            and plan.annual_price is not None
            else plan.monthly_price
        )
        return CreatedPlan(
            id=plan.pk,
            name=plan.name,
            value=value,
            currency=plan.currency,
            # The day-of-month the provider bills on. Derived from when the current
            # period started rather than stored separately — `Subscription` carries
            # no standalone `billing_day` field. Clamped to 28: both MercadoPago and
            # Stripe reject or mishandle billing_day > 28 for monthly recurrence (not
            # every month has a 29th/30th/31st), so a period that started on one of
            # those days bills on the 28th instead of failing the provider call
            # outright.
            billing_day=min(subscription.current_period_start.day, 28),
            # Required since the Stripe adapter was added: `Plan` no longer defaults to
            # a monthly cadence, because that silently made annual plans impossible.
            # Sourced from the subscription rather than the catalog plan — the same
            # plan can be sold monthly or annually.
            billing_interval=subscription.billing_interval,
            external_id=subscription.plan_external_id,
        )
