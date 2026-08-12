"""Rendering the errors this package raises as HTTP responses.

Every error in :mod:`billing.exceptions` already knows its own machine-readable
``code`` and can render itself through ``as_error_body()``. What it does not know
is the status code, because that is an HTTP concern -- so the mapping lives here,
in one table, next to the DRF exception handler that reads it.

Wire it up in settings::

    REST_FRAMEWORK = {
        'EXCEPTION_HANDLER': 'billing.exception_handling.billing_exception_handler',
    }

A project that already has its own handler should call this one from it rather
than replace it::

    def my_exception_handler(exc, context):
        response = billing_exception_handler(exc, context)
        if response is not None:
            return response
        ...  # the project's own mapping

Anything that is not a :class:`~billing.exceptions.BillingError` falls straight
through to DRF's own handler, so installing this changes nothing about how the
rest of a project's errors render.

**Why a table and not a ``status_code`` attribute on each exception.** The
services raise these errors from management commands and background jobs too,
where there is no request and no status code to carry. Keeping the HTTP mapping
out of the exception tree is what lets ``billing.exceptions`` stay importable
without DRF.
"""

from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from billing.exceptions import (
    AddOnNotPurchasableError,
    BillingError,
    ChargeDeclinedError,
    CollectionNotSupportedError,
    IncompleteBillingPlanError,
    MissingBillingProfileError,
    NoDefaultBillingPlanError,
    NoOutstandingBalanceError,
    OverLimitError,
    PaymentProviderNotConfiguredError,
    PaymentTokenRequiredError,
    RetryPaymentNotApplicableError,
    SubscriptionNotAttachedError,
    UnconfirmedPlanChangeError,
    UnknownPaymentProviderError,
)


#: Status code per error class, most specific first -- the lookup walks this in
#: order and takes the first class the exception is an instance of, so a subclass
#: listed above its parent wins.
#:
#: The three families:
#:
#: * **402 Payment Required** -- the payer has to do something with money before
#:   the request can succeed: a ceiling needs a bigger plan or an add-on
#:   (``OverLimitError``), or the instrument on file was declined
#:   (``ChargeDeclinedError``).
#: * **409 Conflict** -- the request is well-formed but the subscription is not
#:   in a state where it means anything: nothing is owed, nothing is attached,
#:   another plan change is already pending, this provider cannot collect.
#: * **400 Bad Request** -- the request itself is missing something the caller
#:   controls (a payment token, a purchasable add-on).
#:
#: Deployment faults (``PaymentProviderNotConfiguredError``,
#: ``NoDefaultBillingPlanError``, ``IncompleteBillingPlanError``) render as
#: **503**: nothing the caller sent is wrong, and a 4xx would tell them to change
#: a request that will fail identically until an operator fixes the deployment.
BILLING_ERROR_STATUS: tuple[tuple[type[BillingError], int], ...] = (
    (OverLimitError, status.HTTP_402_PAYMENT_REQUIRED),
    (ChargeDeclinedError, status.HTTP_402_PAYMENT_REQUIRED),
    (UnknownPaymentProviderError, status.HTTP_404_NOT_FOUND),
    (PaymentProviderNotConfiguredError, status.HTTP_503_SERVICE_UNAVAILABLE),
    (NoDefaultBillingPlanError, status.HTTP_503_SERVICE_UNAVAILABLE),
    (IncompleteBillingPlanError, status.HTTP_503_SERVICE_UNAVAILABLE),
    (UnconfirmedPlanChangeError, status.HTTP_409_CONFLICT),
    (RetryPaymentNotApplicableError, status.HTTP_409_CONFLICT),
    (SubscriptionNotAttachedError, status.HTTP_409_CONFLICT),
    (NoOutstandingBalanceError, status.HTTP_409_CONFLICT),
    (CollectionNotSupportedError, status.HTTP_409_CONFLICT),
    (MissingBillingProfileError, status.HTTP_409_CONFLICT),
    (PaymentTokenRequiredError, status.HTTP_400_BAD_REQUEST),
    (AddOnNotPurchasableError, status.HTTP_400_BAD_REQUEST),
)

#: What a ``BillingError`` with no entry above renders as. A 500 rather than a
#: 400: an unmapped error is one nobody decided a status for, and reporting it as
#: the caller's fault would hide that.
DEFAULT_BILLING_ERROR_STATUS = status.HTTP_500_INTERNAL_SERVER_ERROR


def billing_error_status(exc: BillingError) -> int:
    """The status code ``exc`` renders as."""
    for error_class, code in BILLING_ERROR_STATUS:
        if isinstance(exc, error_class):
            return code
    return DEFAULT_BILLING_ERROR_STATUS


def billing_exception_handler(exc: Exception, context: Any) -> Response | None:
    """DRF exception handler that renders ``BillingError`` through its own body.

    Returns ``None`` for anything DRF itself cannot render either, matching the
    contract DRF's own handler has, so this composes with a project's handler in
    either direction.
    """
    if isinstance(exc, BillingError):
        return Response(exc.as_error_body(), status=billing_error_status(exc))
    return drf_exception_handler(exc, context)
