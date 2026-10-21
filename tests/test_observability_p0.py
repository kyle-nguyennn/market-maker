"""P0 observability/reconciliation/supervisor tests; offline, no credentials."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal as D
from json import loads
from threading import Event

import pytest

from execution.order_manager import LiveOrder, OrderManager, OrderState
from model.order import OrderEvent, OrderEventType, OrderIntent, Side
from observability.events import EventScope, OperationalEvent
from observability.journal import AsyncEventJournal, JsonlSink
from observability.reconciliation import (
    AccountReconciler, LedgerSnapshot, ReconcileResult, VenueSnapshot,
)
from observability.supervisor import SafetyPolicy, SafetySupervisor, TradingState
from observability.watchdog import inspect_heartbeat, write_heartbeat
from risk.risk_manager import RiskDecision

NOW = datetime(2026, 10, 21, 2, 0, tzinfo=UTC)
SCOPE = EventScope("hyperliquid", "pas", "0x" + "a" * 40)
SIG = EventScope("sig", "predictions-cup", "account-a", "tournament-1")


def manager(scope=SCOPE):
    return OrderManager(session_id="ops-tests", dex=scope.environment,
                        account_id=scope.account_id)


def ledger(scope=SCOPE, *, pos=None, cash="1000", fills=frozenset(),
           coverage="2026-10-21:full"):
    return LedgerSnapshot(scope, pos if pos is not None else {"BTC": D(0)},
                          D(cash), frozenset(fills), coverage)


def venue(scope=SCOPE, *, pos=None, cash="1000", fills=frozenset(),
          coverage="2026-10-21:full", at=NOW, orders=(),
          closed=frozenset(), complete=True):
    return VenueSnapshot(scope, pos if pos is not None else {"BTC": D(0)},
                         D(cash), frozenset(fills), coverage, at,
                         tuple(orders), frozenset(closed), complete)


def test_scope_isolation_and_lossless_safe_event():
    assert not SCOPE.same_account(EventScope("hyperliquid", "par", SCOPE.account_id))
    assert not SIG.same_account(EventScope("sig", "predictions-cup", "account-a", "another"))
    e = OperationalEvent(
        scope=SCOPE, event_type="order_intent", correlation_id="c1",
        payload={"price": D("0.000000001000"), "api_key": "NEVER_LOG",
                 "details": {"Authorization": "Bearer secret"},
                 "at": datetime(2026, 10, 21, 10, tzinfo=timezone(timedelta(hours=8)))},
        occurred_at=NOW, event_id="event-1", config_hash="sha-of-config",
    )
    payload = loads(e.to_json())
    assert payload["schema_version"] == 1
    assert payload["payload"]["price"] == "0.000000001000"
    assert payload["payload"]["api_key"] == "<redacted>"
    assert payload["payload"]["details"]["Authorization"] == "<redacted>"
    assert payload["payload"]["at"] == NOW.isoformat()
    assert SCOPE.account_id not in e.to_json()
    assert "NEVER_LOG" not in e.to_json()
    assert payload["scope"]["environment"] == "pas"


@pytest.mark.parametrize("bad", [D("NaN"), D("Infinity"), float("nan"), object()])
def test_event_invalid_payload_fails_closed(bad):
    with pytest.raises((ValueError, TypeError)):
        OperationalEvent(SCOPE, "metric", "trace", {"x": bad}).to_json()


def test_journal_persists_jsonl_without_calling_disk_from_publisher(tmp_path):
    sink = JsonlSink(tmp_path / "ops" / "events.jsonl")
    journal = AsyncEventJournal(sink, capacity=4)
    try:
        assert journal.publish(OperationalEvent(SCOPE, "risk", "c1", {"loss": D("12.3")}))
        journal.drain()
        assert journal.stats().accepted == 1
        assert journal.stats().written == 1
        lines = (tmp_path / "ops" / "events.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert loads(lines[0])["payload"]["loss"] == "12.3"
    finally:
        journal.close()


def test_journal_backpressure_is_bounded_and_reported():
    started, release = Event(), Event()
    written = []
    def blocking_sink(line):
        started.set()
        release.wait(timeout=3)
        written.append(line)
    journal = AsyncEventJournal(blocking_sink, capacity=1)
    try:
        assert journal.publish(OperationalEvent(SCOPE, "market", "1", {}))
        assert started.wait(timeout=3)
        assert journal.publish(OperationalEvent(SCOPE, "market", "2", {}))
        assert not journal.publish(OperationalEvent(SCOPE, "market", "3", {}))
        assert journal.stats().dropped == 1
    finally:
        release.set()
        journal.close()
    assert len(written) == 2


def test_journal_sink_failure_is_visible_and_never_leaks_error():
    def broken(_):
        raise RuntimeError("SECRET FROM CONNECTOR")
    journal = AsyncEventJournal(broken)
    try:
        assert journal.publish(OperationalEvent(SCOPE, "market", "1", {}))
        journal.drain()
        assert journal.stats().failed
        assert not journal.publish(OperationalEvent(SCOPE, "market", "2", {}))
        assert journal.stats().dropped >= 2
    finally:
        journal.close()


def test_reconciler_verifies_cash_positions_fills_and_order_state():
    mm = manager()
    recon = AccountReconciler(mm, SCOPE)
    good = recon.verify(ledger(), venue(), now=NOW)
    assert good == ReconcileResult(True, "reconciled")
    assert mm.ready
    cases = [
        (venue(pos={"BTC": D("0.01")}), "position_mismatch"),
        (venue(cash="999"), "cash_mismatch"),
        (venue(fills={"trade-1"}), "unreconciled_fills"),
        (venue(coverage="missing-day"), "unmatched_fill_coverage"),
        (venue(complete=False), "incomplete_venue_snapshot"),
        (venue(at=NOW - timedelta(seconds=40)), "stale_or_future_snapshot"),
        (venue(scope=EventScope("hyperliquid", "par", SCOPE.account_id)), "scope_mismatch"),
    ]
    for observed, expected in cases:
        result = recon.verify(ledger(), observed, now=NOW)
        assert not result.verified
        assert result.reason == expected
        assert not mm.ready
        assert mm.reconcile([OrderIntent("BTC", Side.BID, D("100"), D("1"))]) == []


def test_reconciliation_unknown_send_never_inferred_cancelled():
    mm = manager()
    recon = AccountReconciler(mm, SCOPE)
    assert recon.verify(ledger(), venue(), now=NOW).verified
    actions = mm.reconcile([OrderIntent("BTC", Side.BID, D("100"), D("1"))])
    cloid = actions[0].intent.client_order_id
    mm.on_order_update(OrderEvent(cloid, OrderEventType.UNKNOWN, dex="pas",
                                  account_id=SCOPE.account_id))
    assert not mm.ready
    unresolved = recon.verify(ledger(), venue(), now=NOW)
    assert not unresolved.verified and not mm.ready
    assert "absent" in unresolved.reason
    # Only explicit authoritative confirmation closes the uncertain order.
    confirmed = recon.verify(ledger(), venue(closed={cloid}), now=NOW)
    assert confirmed.verified
    assert mm.live_orders[cloid].state is OrderState.CANCELLED


def test_reconciler_rejects_foreign_live_order_even_with_matching_balances():
    mm = manager()
    recon = AccountReconciler(mm, SCOPE)
    foreign = LiveOrder("0x" + "f" * 32, 8, "BTC", Side.BID, D("100"),
                        D("1"), D("1"), OrderState.RESTING)
    result = recon.verify(ledger(), venue(orders=(foreign,)), now=NOW)
    assert not result.verified
    assert "foreign" in result.reason
    assert not mm.ready


class Clock:
    def __init__(self):
        self.at = 1000.0
    def __call__(self):
        return self.at
    def advance(self, seconds):
        self.at += seconds


def supervisor(clock, mm):
    s = SafetySupervisor(
        mm, SCOPE, SafetyPolicy(5, 10, 20),
        pretrade_check=lambda *_: RiskDecision(True),
        risk_killed=lambda: False,
        clock=clock,
    )
    return s


def fresh_start(s, mm, clock):
    s.on_market_update(SCOPE, valid=True)
    s.on_bot_heartbeat(SCOPE)
    mm.resynchronize([], positions_reconciled=True, fills_reconciled=True)
    s.on_reconciled(SCOPE, ReconcileResult(True, "reconciled"))
    assert s.arm(operator_authorized=True).allowed
    assert s.state is TradingState.ACTIVE


def test_supervisor_requires_fresh_sync_explicit_arm_and_pauses_on_stale_feed():
    clock = Clock()
    mm = manager()
    s = supervisor(clock, mm)
    assert not s.arm(operator_authorized=True).allowed
    fresh_start(s, mm, clock)
    assert s.authorize_placement(OrderIntent("BTC", Side.BID, D("100"), D("1")), None).allowed
    clock.advance(6)
    assert not s.authorize_placement(OrderIntent("BTC", Side.BID, D("100"), D("1")), None).allowed
    assert s.state is TradingState.PAUSED
    assert not mm.ready
    s.on_market_update(SCOPE, valid=True)
    s.on_bot_heartbeat(SCOPE)
    assert not s.arm(operator_authorized=True).allowed
    # It requires NEW reconciliation after the stale-feed pause.
    mm.resynchronize([], positions_reconciled=True, fills_reconciled=True)
    s.on_reconciled(SCOPE, ReconcileResult(True, "reconciled"))
    assert not s.arm(operator_authorized=False).allowed
    assert s.arm(operator_authorized=True).allowed


def test_supervisor_scope_mismatch_invalid_book_and_kill_switch():
    clock, mm = Clock(), manager()
    s = supervisor(clock, mm)
    fresh_start(s, mm, clock)
    s.on_market_update(EventScope("hyperliquid", "par", SCOPE.account_id), valid=True)
    assert not s.check().allowed and s.reason == "wrong_signal_scope"
    mm.resynchronize([], positions_reconciled=True, fills_reconciled=True)
    s.on_reconciled(SCOPE, ReconcileResult(True, "reconciled"))
    assert s.arm(operator_authorized=True).allowed
    s.on_market_update(SCOPE, valid=False)
    assert s.reason == "invalid_market_data"
    assert not s.check().allowed

    killed = {"value": False}
    mm2 = manager()
    s2 = SafetySupervisor(mm2, SCOPE, SafetyPolicy(5, 10, 20),
                          pretrade_check=lambda *_: RiskDecision(True),
                          risk_killed=lambda: killed["value"], clock=clock)
    fresh_start(s2, mm2, clock)
    killed["value"] = True
    assert not s2.authorize_placement(OrderIntent("BTC", Side.BID, D("1"), D("1")), None).allowed
    assert s2.reason == "risk_kill_switch"


def test_supervisor_does_not_dispatch_cancel_or_expose_unknown_as_safe():
    clock, mm = Clock(), manager()
    s = supervisor(clock, mm)
    fresh_start(s, mm, clock)
    actions = mm.reconcile([OrderIntent("BTC", Side.BID, D("100"), D("1"))])
    cloid = actions[0].intent.client_order_id
    mm.on_order_update(OrderEvent(cloid, OrderEventType.RESTING,
                                  exchange_order_id=10, dex="pas",
                                  account_id=SCOPE.account_id))
    s.pause("operator_pause")
    planned = s.request_safe_cancels()
    assert len(planned) == 1 and planned[0].client_order_id == cloid
    assert mm.live_orders[cloid].state is OrderState.PENDING_CANCEL
    assert s.request_safe_cancels() == []
    assert not mm.ready


def test_watchdog_detects_missing_stale_future_corrupt_and_wrong_scope(tmp_path):
    path = tmp_path / "heartbeat.json"
    assert inspect_heartbeat(path, SCOPE, now=NOW).reason == "missing_heartbeat"
    write_heartbeat(path, SCOPE, now=NOW)
    assert inspect_heartbeat(path, SCOPE, now=NOW).healthy
    assert inspect_heartbeat(path, SCOPE, now=NOW + timedelta(seconds=31)).reason == "stale_or_future_heartbeat"
    assert inspect_heartbeat(path, SCOPE, now=NOW - timedelta(seconds=4)).reason == "stale_or_future_heartbeat"
    assert inspect_heartbeat(path, EventScope("hyperliquid", "par", SCOPE.account_id),
                             now=NOW).reason == "scope_or_schema_mismatch"
    assert SCOPE.account_id not in path.read_text()
    path.write_text("invalid JSON")
    assert inspect_heartbeat(path, SCOPE, now=NOW).reason == "invalid_heartbeat"


def test_sig_tournament_scoping_without_hyperliquid_assumptions():
    # SIG has its own venue adapter and risk check; this basic account/scope
    # reconciler has no perps-specific margin, funding, or mark assumptions.
    mm = manager(SIG)
    recon = AccountReconciler(mm, SIG)
    local = ledger(SIG, pos={"SIG_EXCHANGE:123": D("20")}, cash="980")
    remote = venue(SIG, pos={"SIG_EXCHANGE:123": D("20")}, cash="980")
    assert recon.verify(local, remote, now=NOW).verified
    other = venue(EventScope("sig", "predictions-cup", "account-a", "tournament-2"),
                  pos={"SIG_EXCHANGE:123": D("20")}, cash="980")
    assert not recon.verify(local, other, now=NOW).verified
