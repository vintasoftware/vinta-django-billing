"""The DRF exception handler the package ships.

Before this existed, every error class documented itself as "rendered centrally
as a 402/409" and the central renderer lived in the application this package was
extracted from -- so a project installing the library got a 500 for all of them
until it hand-rolled the mapping. These tests pin the mapping down and, more
importantly, assert that *every* error class has one.
"""

import re

import pytest
from rest_framework import status
from rest_framework.exceptions import NotFound

from vinta_billing import exceptions as billing_exceptions
from vinta_billing.exception_handling import (
    BILLING_ERROR_STATUS,
    DEFAULT_BILLING_ERROR_STATUS,
    billing_error_status,
    billing_exception_handler,
)
from vinta_billing.exceptions import (
    AddOnNotPurchasableError,
    BillingError,
    ChargeDeclinedError,
    NoOutstandingBalanceError,
    OverLimitError,
    PaymentProviderNotConfiguredError,
    UnknownPaymentProviderError,
)


def test_a_billing_error_renders_its_own_body():
    exc = UnknownPaymentProviderError("not-a-provider")

    response = billing_exception_handler(exc, {})

    assert response is not None
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.data == exc.as_error_body()
    assert response.data["code"] == exc.code


def test_a_declined_charge_is_payment_required():
    exc = ChargeDeclinedError(1, "card_declined")

    response = billing_exception_handler(exc, {})

    assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED


def test_an_over_limit_error_is_payment_required_and_keeps_its_fields():
    exc = OverLimitError.from_missing_entitlement("white_label")

    response = billing_exception_handler(exc, {})

    assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
    assert set(response.data) >= {"code", "detail", "current_usage", "limit", "remedy"}


def test_a_deployment_fault_is_service_unavailable_not_a_4xx():
    """Nothing the caller sent is wrong, and a 4xx would tell them to change a
    request that will fail identically until an operator fixes the deployment."""
    exc = PaymentProviderNotConfiguredError("stripe")

    response = billing_exception_handler(exc, {})

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_a_state_conflict_is_409():
    assert billing_error_status(NoOutstandingBalanceError(1)) == status.HTTP_409_CONFLICT


def test_a_caller_input_problem_is_400():
    assert billing_error_status(AddOnNotPurchasableError("widgets")) == (
        status.HTTP_400_BAD_REQUEST
    )


def test_a_non_billing_exception_falls_through_to_drf():
    response = billing_exception_handler(NotFound("nope"), {})

    assert response is not None
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_something_drf_cannot_render_either_returns_none():
    """Matching DRF's own handler contract, so this composes in either
    direction with a project's handler."""
    assert billing_exception_handler(RuntimeError("boom"), {}) is None


def test_an_unmapped_billing_error_is_a_500_not_a_400():
    """An error nobody decided a status for is a gap in this table, and
    reporting it as the caller's fault would hide that."""

    class BrandNewError(BillingError):
        code = "brand_new"

    assert billing_error_status(BrandNewError("x")) == DEFAULT_BILLING_ERROR_STATUS
    assert DEFAULT_BILLING_ERROR_STATUS == status.HTTP_500_INTERNAL_SERVER_ERROR


def _concrete_error_classes():
    """Every ``BillingError`` subclass the package defines, minus the bases that
    exist only to be inherited from."""
    bases = {
        billing_exceptions.BillingError,
        billing_exceptions.PaymentError,
        billing_exceptions.PaymentAdapterError,
    }
    return sorted(
        (
            obj
            for obj in vars(billing_exceptions).values()
            if isinstance(obj, type) and issubclass(obj, BillingError) and obj not in bases
        ),
        key=lambda cls: cls.__name__,
    )


@pytest.mark.parametrize("error_class", _concrete_error_classes(), ids=lambda cls: cls.__name__)
def test_every_error_class_has_a_deliberate_status(error_class):
    """Fails when a new error class is added without deciding what it renders as.

    Some classes legitimately have no HTTP meaning -- they are raised from the
    meter or a management command and never reach a view. Those are listed here
    explicitly rather than left to fall through to the 500 default, so the list
    is a record of a decision rather than an oversight.
    """
    NOT_HTTP_FACING = {
        "BillingPeriodResolutionError",
        "BillingProfileContactEmailMissingError",
        "BillingRootCycleError",
        "IllegalBillingStateTransitionError",
        # A call site naming a usage_extra key the resource does not read is a
        # programming error, like InvalidLimitCheckResultError below: there is
        # nothing a client did wrong and nothing it could do differently, so this
        # falls through to the 500 default rather than getting a 4xx that would
        # invite a retry.
        "InapplicableUsageExtraError",
        "InvalidLimitCheckResultError",
        "MissingSeedBillingPlanError",
        "ProviderWebhookEventIdMissingError",
        "SubscriptionExternalIdMissingInNotificationError",
    }
    mapped = {cls for cls, _ in BILLING_ERROR_STATUS}

    if error_class.__name__ in NOT_HTTP_FACING:
        assert error_class not in mapped, (
            f"{error_class.__name__} is listed as not HTTP-facing but has a status; "
            "remove it from NOT_HTTP_FACING."
        )
        return

    assert any(issubclass(error_class, mapped_class) for mapped_class in mapped), (
        f"{error_class.__name__} has no entry in BILLING_ERROR_STATUS and is not "
        "listed as non-HTTP-facing -- decide which it is."
    )


def _declared_error_statuses():
    """``(path, method, status, error_class_name)`` for every error class the
    shipped OpenAPI annotations name in a response description.

    Generated from the real schema through the real urlconf, not read off the
    decorators: what an adopter generates a client from is the rendered
    document, and that is what has to agree with ``BILLING_ERROR_STATUS``.
    """
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator(urlconf="tests.urls").get_schema(request=None, public=True)

    declared = []
    for path, operations in sorted(schema["paths"].items()):
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in sorted(operation.get("responses", {}).items()):
                description = response.get("description") or ""
                for name in sorted(set(re.findall(r"\b([A-Z][A-Za-z]*Error)\b", description))):
                    declared.append((path, method.upper(), int(code), name))
    return declared


def test_the_published_schema_agrees_with_the_status_table():
    """The bug this exists to stop: through 0.5.0 three shipped annotations
    declared ``PaymentProviderNotConfiguredError`` under **409** while
    ``BILLING_ERROR_STATUS`` rendered it **503**. Both were deliberate, which is
    why neither side noticed -- and a client generated from the published schema
    handled a status this API never returns, then met a 503 it had no branch
    for.

    Every error class an annotation names has to be listed under the status the
    handler actually returns for it. A description that mentions a class only to
    contrast with it still has to name the right status, which is a feature: a
    cross-reference that has gone stale is exactly as misleading as a wrong
    declaration.
    """
    pytest.importorskip("drf_spectacular")

    declared = _declared_error_statuses()
    assert declared, "no error classes are named in any response description -- check the regex"

    disagreements = []
    for path, method, code, name in declared:
        error_class = getattr(billing_exceptions, name, None)
        if error_class is None:
            disagreements.append(f"{method} {path} {code}: names {name}, which does not exist")
            continue
        expected = billing_error_status(error_class.__new__(error_class))
        if expected != code:
            disagreements.append(
                f"{method} {path}: declares {name} under {code}, "
                f"but BILLING_ERROR_STATUS renders it {expected}"
            )

    assert not disagreements, "schema and status table disagree:\n  " + "\n  ".join(disagreements)
