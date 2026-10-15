"""Which status a charge or a refund may move to, and the one place each is written.

Providers deliver at least once and in no particular order, and the same status
often arrives more than once (MercadoPago notifies on every change to a payment,
not only on status changes). So a provider update is a *claim* about the status,
checked against these rules before it overwrites the one already recorded.

**Charges** (``Payment.status``):

1. **The same status again is not a change.** Nothing is written beyond the raw
   ``original_status``, and ``payment_status_changed`` is not sent.
2. **A final status never moves.** ``rejected``, ``rejected_by_bank``,
   ``cancelled``, ``expired``, ``refunded`` and ``charged_back`` are where a
   charge ends; anything arriving afterwards is stale.
3. **A settled charge only moves forward.** ``approved`` can still become
   ``partially_refunded``, ``refunded``, ``charged_back`` or ``in_mediation``,
   and those intermediate states can still resolve (a dispute can be decided
   for the seller, back to ``approved``) -- see
   ``SETTLED_PAYMENT_STATUS_SUCCESSORS``. A settled charge never goes back to
   ``pending`` or ``in_process``.
4. **An open charge can move anywhere** a provider can report, except back to
   ``pending_send`` (which only this package writes, before the provider has
   heard of the charge) and to ``unknown`` once the provider has reported a
   status it understood: an unmapped status says nothing new and must not
   overwrite one that did.

**Refunds** (``Refund.status``) follow the same shape with a smaller vocabulary:
``approved`` and ``rejected`` are final, ``unknown`` never overwrites a status
the provider reported, and nothing returns to ``pending_send``.

A refused update is still kept as a ``PaymentStatusUpdate`` row by its caller --
the history of what the provider said -- but does not touch the charge's
status, so the signal is never sent for it.
"""

import logging

from django.db import transaction

from vinta_billing.constants import PaymentStatuses, RefundStatuses
from vinta_billing.models import Payment, Refund
from vinta_billing.signals import payment_status_changed, refund_status_changed, send_on_commit


logger = logging.getLogger(__name__)


#: Statuses a charge ends in. Nothing moves a charge out of one.
FINAL_PAYMENT_STATUSES: frozenset[str] = frozenset(
    {
        PaymentStatuses.REJECTED,
        PaymentStatuses.REJECTED_BY_BANK,
        PaymentStatuses.CANCELLED,
        PaymentStatuses.EXPIRED,
        PaymentStatuses.REFUNDED,
        PaymentStatuses.CHARGED_BACK,
    }
)

#: Where a charge that already collected money may still go.
SETTLED_PAYMENT_STATUS_SUCCESSORS: dict[str, frozenset[str]] = {
    PaymentStatuses.APPROVED: frozenset(
        {
            PaymentStatuses.PARTIALLY_REFUNDED,
            PaymentStatuses.REFUNDED,
            PaymentStatuses.CHARGED_BACK,
            PaymentStatuses.IN_MEDIATION,
        }
    ),
    PaymentStatuses.IN_MEDIATION: frozenset(
        {
            PaymentStatuses.APPROVED,
            PaymentStatuses.PARTIALLY_REFUNDED,
            PaymentStatuses.REFUNDED,
            PaymentStatuses.CHARGED_BACK,
        }
    ),
    PaymentStatuses.PARTIALLY_REFUNDED: frozenset(
        {
            PaymentStatuses.REFUNDED,
            PaymentStatuses.CHARGED_BACK,
            PaymentStatuses.IN_MEDIATION,
        }
    ),
}

#: Statuses a refund ends in.
FINAL_REFUND_STATUSES: frozenset[str] = frozenset(
    {RefundStatuses.APPROVED, RefundStatuses.REJECTED}
)


def can_move_payment_status(from_status: str, to_status: str) -> bool:
    """Whether a provider update may move a charge from ``from_status`` to
    ``to_status``. ``False`` for the same status: that is not a move. See the
    module docstring for the rules."""
    if from_status == to_status:
        return False
    if to_status == PaymentStatuses.PENDING_SEND:
        return False
    if from_status in FINAL_PAYMENT_STATUSES:
        return False
    if from_status in SETTLED_PAYMENT_STATUS_SUCCESSORS:
        return to_status in SETTLED_PAYMENT_STATUS_SUCCESSORS[from_status]
    if to_status == PaymentStatuses.UNKNOWN:
        return from_status == PaymentStatuses.PENDING_SEND
    return True


def can_move_refund_status(from_status: str, to_status: str) -> bool:
    """Whether a refund may move from ``from_status`` to ``to_status``. See the
    module docstring for the rules."""
    if from_status == to_status:
        return False
    if to_status == RefundStatuses.PENDING_SEND:
        return False
    if from_status in FINAL_REFUND_STATUSES:
        return False
    if to_status == RefundStatuses.UNKNOWN:
        return from_status == RefundStatuses.PENDING_SEND
    return True


def record_payment_status(payment: Payment, to_status: str, original_status: str = "") -> bool:
    """Move ``payment`` to ``to_status`` if the rules allow it, and announce it.

    Returns whether ``Payment.status`` changed. When it did, the row is written
    and ``payment_status_changed`` is queued for commit, in the caller's
    transaction; ``payment`` is updated in place either way, so the caller sees
    what is stored.

    The row is re-read under ``SELECT ... FOR UPDATE`` first, so two deliveries
    of the same news racing each other cannot both read the old status and both
    announce the change: the second waits for the first to commit, then sees the
    status already moved and does nothing.

    ``original_status`` is the provider's raw status. It is stored whenever the
    mapped status is accepted or unchanged, so it always describes ``status``;
    a refused update leaves both alone.
    """
    with transaction.atomic():
        current = (
            Payment.objects.select_for_update().only("status", "original_status").get(pk=payment.pk)
        )
        from_status = current.status
        payment.status = from_status
        payment.original_status = current.original_status

        if from_status == to_status:
            if original_status and original_status != current.original_status:
                payment.original_status = original_status
                payment.save(update_fields=["original_status", "modified"])
            return False

        if not can_move_payment_status(from_status, to_status):
            logger.info(
                "Ignored payment status update: payment_id=%s from_status=%s to_status=%s",
                payment.pk,
                from_status,
                to_status,
            )
            return False

        payment.status = to_status
        update_fields = ["status", "modified"]
        if original_status:
            payment.original_status = original_status
            update_fields.append("original_status")
        payment.save(update_fields=update_fields)
        send_on_commit(
            payment_status_changed,
            sender=Payment,
            payment=payment,
            from_status=from_status,
            to_status=to_status,
        )
    return True


def record_refund_status(refund: Refund, to_status: str) -> bool:
    """Move ``refund`` to ``to_status`` if the rules allow it, and announce it.

    The refund-side twin of ``record_payment_status``, with the same locking and
    the same return value. ``refund`` must already be saved.
    """
    with transaction.atomic():
        from_status = (
            Refund.objects.select_for_update().values_list("status", flat=True).get(pk=refund.pk)
        )
        refund.status = from_status
        if not can_move_refund_status(from_status, to_status):
            if from_status != to_status:
                logger.info(
                    "Ignored refund status update: refund_id=%s from_status=%s to_status=%s",
                    refund.pk,
                    from_status,
                    to_status,
                )
            return False

        refund.status = to_status
        refund.save(update_fields=["status", "modified"])
        send_on_commit(
            refund_status_changed,
            sender=Refund,
            refund=refund,
            from_status=from_status,
            to_status=to_status,
        )
    return True
