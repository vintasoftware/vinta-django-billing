"""Resolves which payment provider governs an organization's *future* charges.

A sibling of ``billing.services.payment_service`` rather than a method on
``PaymentService`` itself: ``PaymentService`` is generic over the adapter types
(``PaymentAdapter``/``SubscriptionAdapter``/``SubscriptionPlanFactory``), while this
resolver has zero dependency on any adapter -- it only reads the configured default
provider and ``BillingProfile.payment_provider``. Keeping it separate means the read
path can resolve a provider without importing the adapter stack at all.

Single place both the provider-credentials endpoints
(``billing.views.PaymentProviderViewSet``) and charge routing
(``PaymentService.create_payment``/``create_subscription``) call to resolve an
organization's provider -- so the pin -> default resolution rule cannot drift between the
read path and the write path.
"""

import logging

from organizations.models import Organization

from billing.conf import get_setting
from billing.models import BillingProfile


logger = logging.getLogger(__name__)


class PaymentProviderResolver:
    """Resolves the payment provider governing an organization's next charge.

    Stateless -- reads ``VINTA_BILLING['DEFAULT_PROVIDER']`` and the organization's own
    ``BillingProfile.payment_provider`` pin. No adapter or secret credential is reachable
    from this class.
    """

    def resolve_for_organization(self, organization: Organization) -> str:
        """The provider governing ``organization``'s next charge: its pin when non-empty,
        ``VINTA_BILLING['DEFAULT_PROVIDER']`` otherwise.

        An organization with no ``BillingProfile`` at all resolves to the default, exactly
        like one whose profile exists but carries an empty pin -- see
        ``BillingProfile.payment_provider``'s docstring: ``""`` means both "never paid" and
        "explicitly un-pinned by staff", and both cases resolve identically. Callers must
        not try to distinguish the two.
        """
        try:
            billing_profile = organization.billing_profile
        except BillingProfile.DoesNotExist:
            return self.resolve_default()
        return billing_profile.payment_provider or self.resolve_default()

    def resolve_default(self) -> str:
        """The system-wide default provider."""
        return str(get_setting("DEFAULT_PROVIDER"))
