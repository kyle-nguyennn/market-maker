"""Deterministic, offline order lifecycle tests. No SDK, keys or network."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from execution.order_manager import (
    LiveOrder, OrderManager, OrderState, ReconciliationError,
)
from model.order import (
    CancelAction, OrderEvent, OrderEventType as E, OrderIntent, PlaceAction, Side,
)


def intent(side=Side.BID, price="100", size="1"):
    return OrderIntent("BTC", side, D(price), D(size))


def manager():
    mm = OrderManager(session_id="repeatable-test-session", dex="par", account_id="0x" + "a" * 40)
    assert not mm.ready
    assert mm.resynchronize([], positions_reconciled=True, fills_reconciled=True)
    return mm


def emit(mm, cloid, kind, *, cumulative=None, oid=7, dex="par", account=None):
    return mm.on_order_update(OrderEvent(
        client_order_id=cloid, kind=kind, exchange_order_id=oid,
        cumulative_filled_size=D(cumulative) if cumulative is not None else None,
        instrument="BTC", dex=dex, account_id=account or "0x" + "a" * 40,
    ))


def placed(mm, target=None):
    actions = mm.reconcile([target or intent()])
    assert len(actions) == 1 and isinstance(actions[0], PlaceAction)
    cloid = actions[0].intent.client_order_id
    assert cloid.startswith("0x") and len(cloid) == 34
    return cloid


def resting(mm, cloid):
    emit(mm, cloid, E.RESTING)
    assert mm.live_orders[cloid].state is OrderState.RESTING


def test_cold_start_fail_closed_until_explicit_verified_sync():
    mm = OrderManager(session_id="test")
    assert mm.reconcile([intent()]) == []
    assert not mm.resynchronize([], positions_reconciled=True, fills_reconciled=False)
    assert mm.blocked_reason == "position_or_fills_unverified"
    assert mm.reconcile([intent()]) == []
    assert mm.resynchronize([], positions_reconciled=True, fills_reconciled=True)
    assert len(mm.reconcile([intent()])) == 1


def test_pending_new_is_idempotent_and_cloid_is_128bit():
    mm = manager()
    cloid = placed(mm)
    assert mm.reconcile([intent()]) == []
    assert mm.live_orders[cloid].state is OrderState.PENDING_NEW
    resting(mm, cloid)
    assert mm.reconcile([intent()]) == []
    assert emit(mm, cloid, E.RESTING) == D(0)
    assert len(mm.live_orders) == 1


def test_removed_quote_single_cancel_no_retry_before_terminal():
    mm = manager()
    cloid = placed(mm)
    resting(mm, cloid)
    actions = mm.reconcile([])
    assert actions == [CancelAction("BTC", cloid, 7)]
    assert mm.reconcile([]) == []
    assert emit(mm, cloid, E.CANCEL_ACCEPTED) == D(0)
    assert mm.live_orders[cloid].state is OrderState.PENDING_CANCEL
    assert mm.reconcile([intent()]) == []
    emit(mm, cloid, E.CANCELLED)
    assert mm.live_orders[cloid].state is OrderState.CANCELLED


def test_change_quote_cancel_wait_replace_with_distinct_cloid():
    mm = manager()
    old = placed(mm)
    resting(mm, old)
    replacement = intent(price="101")
    assert mm.reconcile([replacement]) == [CancelAction("BTC", old, 7)]
    assert mm.reconcile([replacement]) == []
    emit(mm, old, E.RESTING)  # A delayed RESTING must not undo pending cancel.
    assert mm.live_orders[old].state is OrderState.PENDING_CANCEL
    emit(mm, old, E.CANCELLED)
    new = placed(mm, replacement)
    assert new != old
    assert mm.reconcile([replacement]) == []


def test_partial_fill_cumulative_not_delta_and_pending_cancel_race():
    mm = manager()
    cloid = placed(mm)
    resting(mm, cloid)
    assert emit(mm, cloid, E.PARTIAL_FILL, cumulative="0.4") == D("0.4")
    assert emit(mm, cloid, E.PARTIAL_FILL, cumulative="0.4") == D("0")
    order = mm.live_orders[cloid]
    assert order.remaining_size == D("0.6")
    assert order.state is OrderState.PARTIALLY_FILLED
    assert mm.reconcile([]) == [CancelAction("BTC", cloid, 7)]
    assert emit(mm, cloid, E.PARTIAL_FILL, cumulative="0.7") == D("0.3")
    assert order.remaining_size == D("0.3")
    assert order.state is OrderState.PENDING_CANCEL
    emit(mm, cloid, E.CANCELLED, cumulative="0.7")
    assert order.state is OrderState.CANCELLED
    assert emit(mm, cloid, E.PARTIAL_FILL, cumulative="0.9") == D("0.2")
    assert order.state is OrderState.CANCELLED  # Late fill cannot reopen.
    assert order.remaining_size == D("0.1")


def test_fully_filled_during_cancel_then_stale_cancelled_no_regression():
    mm = manager()
    cloid = placed(mm)
    resting(mm, cloid)
    mm.reconcile([])
    assert emit(mm, cloid, E.FILLED, cumulative="1") == D("1")
    emit(mm, cloid, E.CANCELLED, cumulative="0.5")
    assert mm.live_orders[cloid].state is OrderState.FILLED
    assert mm.live_orders[cloid].remaining_size == D("0")


def test_ambiguous_submit_timeout_does_not_retry_with_new_identity():
    mm = manager()
    cloid = placed(mm)
    emit(mm, cloid, E.UNKNOWN, oid=None)
    assert not mm.ready
    assert mm.live_orders[cloid].state is OrderState.UNKNOWN
    assert mm.reconcile([intent()]) == []
    # Neither absence in open orders nor repeated desired quote clears unknown.
    assert not mm.resynchronize([], positions_reconciled=True, fills_reconciled=True)
    assert mm.reconcile([intent()]) == []
    snapshot = replace(mm.live_orders[cloid], exchange_order_id=7, state=OrderState.RESTING)
    assert mm.resynchronize([snapshot], positions_reconciled=True, fills_reconciled=True)
    assert mm.reconcile([intent()]) == []
    assert mm.live_orders[cloid].state is OrderState.RESTING


def test_confirmed_closed_status_is_required_to_recover_missing_order():
    mm = manager()
    cloid = placed(mm)
    emit(mm, cloid, E.UNKNOWN, oid=None)
    assert mm.resynchronize([], positions_reconciled=True, fills_reconciled=True,
                            confirmed_closed_cloids=frozenset({cloid}))
    assert mm.live_orders[cloid].state is OrderState.CANCELLED
    assert placed(mm) != cloid


def test_resync_foreign_order_quarantined_without_adoption():
    mm = manager()
    foreign = LiveOrder("0x" + "f" * 32, 88, "BTC", Side.BID, D("100"),
                        D("1"), D("1"), OrderState.RESTING)
    assert not mm.resynchronize([foreign], positions_reconciled=True, fills_reconciled=True)
    assert "foreign" in mm.blocked_reason
    assert foreign.client_order_id not in mm.live_orders
    assert mm.reconcile([intent()]) == []


def test_snapshot_mismatch_quarantines_and_does_not_mutate_order():
    mm = manager()
    cloid = placed(mm)
    resting(mm, cloid)
    original = replace(mm.live_orders[cloid])
    unexpected = replace(original, price=D("102"))
    assert not mm.resynchronize([unexpected], positions_reconciled=True, fills_reconciled=True)
    assert mm.live_orders[cloid] == original
    assert mm.reconcile([intent()]) == []


def test_rest_recovery_rejects_unverified_position_and_fills():
    mm = manager()
    cloid = placed(mm)
    resting(mm, cloid)
    snapshot = replace(mm.live_orders[cloid])
    assert not mm.resynchronize([snapshot], positions_reconciled=False, fills_reconciled=True)
    assert not mm.ready


def test_cross_dex_and_wrong_account_never_mutate_local_state():
    mm = manager()
    cloid = placed(mm)
    for kwargs in ({"dex": "pa"}, {"account": "0x" + "b" * 40}):
        with pytest.raises(ValueError):
            emit(mm, cloid, E.RESTING, **kwargs)
    assert mm.live_orders[cloid].state is OrderState.PENDING_NEW
    assert mm.ready


def test_unknown_cloid_and_oid_conflict_quarantine():
    mm = manager()
    with pytest.raises(ReconciliationError):
        emit(mm, "0x" + "e" * 32, E.RESTING)
    assert not mm.ready
    mm = manager()
    cloid = placed(mm)
    resting(mm, cloid)
    with pytest.raises(ReconciliationError, match="OID"):
        emit(mm, cloid, E.RESTING, oid=8)
    assert not mm.ready


def test_out_of_bounds_and_nan_cumulative_fill_fail_closed():
    mm = manager()
    cloid = placed(mm)
    with pytest.raises(ReconciliationError):
        emit(mm, cloid, E.PARTIAL_FILL, cumulative="1.1")
    assert not mm.ready
    mm = manager()
    cloid = placed(mm)
    with pytest.raises(ReconciliationError):
        emit(mm, cloid, E.PARTIAL_FILL, cumulative="NaN")
    assert not mm.ready


@pytest.mark.parametrize("price,size", [
    ("0", "1"), ("-1", "1"), ("NaN", "1"), ("Infinity", "1"),
    ("100", "0"), ("100", "-1"), ("100", "NaN"),
])
def test_invalid_desired_never_reserved(price, size):
    mm = manager()
    with pytest.raises(ValueError):
        mm.reconcile([intent(price=price, size=size)])
    assert mm.live_orders == {}


def test_duplicate_same_side_desired_is_rejected_before_mutation():
    mm = manager()
    with pytest.raises(ValueError, match="duplicate desired"):
        mm.reconcile([intent(), intent(price="99")])
    assert not mm.live_orders


def test_two_sides_can_place_but_cancels_are_ordered_first():
    mm = manager()
    actions = mm.reconcile([intent(Side.BID), intent(Side.ASK, "102")])
    assert len(actions) == 2
    assert all(isinstance(x, PlaceAction) for x in actions)
    bid_cloid = next(x.intent.client_order_id for x in actions if x.intent.side is Side.BID)
    ask_cloid = next(x.intent.client_order_id for x in actions if x.intent.side is Side.ASK)
    resting(mm, bid_cloid)
    resting(mm, ask_cloid)
    actions = mm.reconcile([intent(Side.BID, "99"), intent(Side.ASK, "102")])
    assert actions == [CancelAction("BTC", bid_cloid, 7)]


def test_emergency_cancel_available_while_recovery_blocks_new_orders():
    mm = manager()
    cloid = placed(mm)
    resting(mm, cloid)
    mm.start_recovery("stale_feed")
    assert mm.reconcile([intent()]) == []
    assert mm.request_cancel_all() == [CancelAction("BTC", cloid, 7)]
    assert mm.request_cancel_all() == []
    emit(mm, cloid, E.CANCEL_ACCEPTED)
    assert mm.live_orders[cloid].state is OrderState.PENDING_CANCEL


def test_deterministic_replay_action_identities():
    def trace():
        mm = manager()
        a = mm.reconcile([intent(Side.ASK, "105"), intent(Side.BID)])[0]
        cloid = a.intent.client_order_id
        emit(mm, cloid, E.RESTING)
        return cloid, mm.reconcile([intent(Side.ASK, "105"), intent(Side.BID)])
    assert trace() == trace()
