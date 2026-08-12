"""Which billing-state transitions are legal.

Every place that changes `Subscription.billing_state` goes through
`transition_billing_state`, so this table is the whole set of moves the system
can make. An edge added here by accident is a customer suspended (or
un-suspended) by accident.
"""

import pytest

from billing.constants import BillingState
from billing.exceptions import IllegalBillingStateTransitionError
from billing.services.billing_state_machine import (
    LEGAL_BILLING_STATE_TRANSITIONS,
    transition_billing_state,
)


pytestmark = pytest.mark.django_db


class TestLegalTransitions:
    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (BillingState.FREE, BillingState.ACTIVE),
            (BillingState.ACTIVE, BillingState.GRACE),
            (BillingState.GRACE, BillingState.ACTIVE),
            (BillingState.GRACE, BillingState.RESTRICTED),
            (BillingState.RESTRICTED, BillingState.ACTIVE),
            (BillingState.ACTIVE, BillingState.CANCELLED),
        ],
    )
    def test_a_legal_edge_is_written(self, subscription, from_state, to_state):
        subscription.billing_state = from_state
        subscription.save(update_fields=["billing_state"])

        transition_billing_state(subscription, to_state)

        subscription.refresh_from_db()
        assert subscription.billing_state == to_state

    def test_recovery_from_restricted_is_reachable(self, subscription):
        """A customer who pays must be able to get back to work.

        The one edge whose absence would be a support emergency rather than a
        bug report.
        """
        assert (
            BillingState.RESTRICTED,
            BillingState.ACTIVE,
        ) in LEGAL_BILLING_STATE_TRANSITIONS

    def test_every_state_can_be_cancelled(self, subscription):
        """The cancel action is offered from every live state, so each needs its
        edge -- beyond the single ACTIVE -> CANCELLED the spec diagram draws."""
        for state in (BillingState.FREE, BillingState.GRACE, BillingState.RESTRICTED):
            assert (state, BillingState.CANCELLED) in LEGAL_BILLING_STATE_TRANSITIONS


class TestIdempotence:
    @pytest.mark.parametrize(
        "state",
        [
            BillingState.FREE,
            BillingState.ACTIVE,
            BillingState.GRACE,
            BillingState.RESTRICTED,
            BillingState.CANCELLED,
        ],
    )
    def test_transitioning_to_the_current_state_is_a_no_op(self, subscription, state):
        """Entering grace twice, or a dunning retry firing twice, must not raise.

        The sweeps are at-least-once by design, so a self-transition is the
        normal case, not an error.
        """
        subscription.billing_state = state
        subscription.save(update_fields=["billing_state"])

        transition_billing_state(subscription, state)

        subscription.refresh_from_db()
        assert subscription.billing_state == state


class TestIllegalTransitions:
    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            # Skipping grace would suspend a customer whose card failed once,
            # with no window to fix it.
            (BillingState.ACTIVE, BillingState.RESTRICTED),
            # Cancelled is terminal until cycle close returns it to FREE.
            (BillingState.CANCELLED, BillingState.ACTIVE),
            (BillingState.CANCELLED, BillingState.GRACE),
            # A free organization has nothing to restrict or grace-recover.
            (BillingState.FREE, BillingState.RESTRICTED),
        ],
    )
    def test_an_illegal_edge_raises(self, subscription, from_state, to_state):
        subscription.billing_state = from_state
        subscription.save(update_fields=["billing_state"])

        with pytest.raises(IllegalBillingStateTransitionError):
            transition_billing_state(subscription, to_state)

    def test_a_refused_transition_leaves_the_row_untouched(self, subscription):
        subscription.billing_state = BillingState.CANCELLED
        subscription.save(update_fields=["billing_state"])

        with pytest.raises(IllegalBillingStateTransitionError):
            transition_billing_state(subscription, BillingState.ACTIVE)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.CANCELLED

    def test_the_error_names_both_ends(self, subscription):
        subscription.billing_state = BillingState.ACTIVE
        subscription.save(update_fields=["billing_state"])

        with pytest.raises(IllegalBillingStateTransitionError) as excinfo:
            transition_billing_state(subscription, BillingState.RESTRICTED)

        # The message interpolates the enum members, so it carries their names
        # (`BillingState.ACTIVE`) rather than their values (`active`).
        message = str(excinfo.value)
        assert BillingState.ACTIVE.name in message
        assert BillingState.RESTRICTED.name in message
        assert str(subscription.pk) in message


class TestTableShape:
    def test_no_edge_leaves_a_state_the_table_cannot_reach(self):
        """Every state that can be entered can also be left.

        A state with inbound edges and no outbound ones is a trap: whatever
        lands there stays forever, and only a data fix gets it out.
        """
        reachable = {to_state for _from, to_state in LEGAL_BILLING_STATE_TRANSITIONS}
        escapable = {from_state for from_state, _to in LEGAL_BILLING_STATE_TRANSITIONS}

        assert reachable - escapable == set()
