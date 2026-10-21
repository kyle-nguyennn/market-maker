"""Offline SQLite WAL durability, deduplication, crash-window and gateway tests."""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import Future
from decimal import Decimal as D

import pytest

from execution.durable_dispatch import DurableExecutionDispatcher
from execution.order_manager import OrderManager
from execution.safe_gateway import GatewayOutcome
from execution.sqlite_journal import (
    JournalConflict, JournalUnavailable, SQLiteWALJournal,
)
from model.order import CancelAction, OrderIntent, PlaceAction, Side


ACCOUNT = "0x" + "a" * 40
CLOID = "0x" + "b" * 32
CLOID2 = "0x" + "c" * 32


def place(cloid=CLOID):
    return PlaceAction(OrderIntent(
        "BTC", Side.BID, D("102.50"), D("0.20"), client_order_id=cloid,
    ))


def journal(tmp_path, *, dex="pas", account=ACCOUNT):
    return SQLiteWALJournal(tmp_path / "state" / "trading.sqlite", dex=dex, account_id=account)


def test_wal_full_and_persistent_identity(tmp_path):
    path = tmp_path / "state" / "trading.sqlite"
    with journal(tmp_path) as j:
        receipt = j.prepare_place(place()).result(timeout=3)
        assert receipt.action_id > 0
        snapshot = j.load_recovery().result(timeout=3)
        assert [o.client_order_id for o in snapshot.unresolved] == [CLOID]
        assert snapshot.unresolved[0].price == D("102.50")
        assert snapshot.unresolved[0].dispatch_state == "PREPARED"
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with journal(tmp_path) as reopened:
        assert len(reopened.load_recovery().result(timeout=3).unresolved) == 1
    with pytest.raises(JournalUnavailable, match="initialization"):
        journal(tmp_path, dex="par")
    with pytest.raises(JournalUnavailable, match="initialization"):
        journal(tmp_path, account="0x" + "f" * 40)


def test_duplicate_cloid_is_rejected_and_original_is_unchanged(tmp_path):
    with journal(tmp_path) as j:
        first = j.prepare_place(place()).result(timeout=3)
        with pytest.raises(JournalConflict, match="already journaled"):
            j.prepare_place(place()).result(timeout=3)
        j.record_place_outcome(first, sent=True, reason="resting").result(timeout=3)
        with pytest.raises(JournalConflict, match="already finalized"):
            j.record_place_outcome(first, sent=True, reason="resting").result(timeout=3)
        rows = j.load_recovery().result(timeout=3).unresolved
        assert len(rows) == 1 and rows[0].dispatch_state == "SENT"


def test_explicit_unsent_action_is_not_ambiguous(tmp_path):
    with journal(tmp_path) as j:
        receipt = j.prepare_place(place()).result(timeout=3)
        j.record_place_outcome(
            receipt, sent=False, reason="stale_market_data",
        ).result(timeout=3)
        assert j.load_recovery().result(timeout=3).unresolved == ()


def test_crash_after_prepare_requires_exchange_reconciliation(tmp_path):
    with journal(tmp_path) as j:
        j.prepare_place(place()).result(timeout=3)
        # Simulated crash window: no dispatch outcome persisted.
    with journal(tmp_path) as reopened:
        recovery = reopened.load_recovery().result(timeout=3)
        assert recovery.unresolved[0].dispatch_state == "PREPARED"
        assert recovery.unresolved[0].exchange_order_id is None
        # Must not infer that a PREPARED action was never sent.


def test_fill_deduplication_and_conflict_detection(tmp_path):
    fill = dict(
        fill_id="venue-fill-1", client_order_id=CLOID, instrument="BTC",
        size=D("0.02"), price=D("102.25"), fee=D("-0.001"),
        event_ts_ms=123456789,
    )
    with journal(tmp_path) as j:
        assert j.record_fill(**fill).result(timeout=3) is True
        assert j.record_fill(**fill).result(timeout=3) is False
        with pytest.raises(JournalConflict, match="conflicting"):
            j.record_fill(**(fill | {"fee": D("0.001")})).result(timeout=3)
        assert j.load_recovery().result(timeout=3).fill_count == 1
    with journal(tmp_path) as reopened:
        assert reopened.record_fill(**fill).result(timeout=3) is False


def test_kill_switch_latched_across_restarts(tmp_path):
    with journal(tmp_path) as j:
        j.latch_kill_switch("drawdown").result(timeout=3)
        j.latch_kill_switch("another").result(timeout=3)
    with journal(tmp_path) as j:
        snapshot = j.load_recovery().result(timeout=3)
        assert snapshot.kill_switch
        assert snapshot.kill_reason == "drawdown"


def test_dex_and_decimal_validation_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="persistent"):
        SQLiteWALJournal(":memory:", dex="pas", account_id=ACCOUNT)
    with journal(tmp_path) as j:
        with pytest.raises(ValueError, match="128-bit"):
            j.prepare_place(place("bad"))
        with pytest.raises(ValueError, match="finite"):
            j.prepare_place(PlaceAction(OrderIntent(
                "BTC", Side.BID, D("NaN"), D("1"), client_order_id=CLOID2,
            )))
        with pytest.raises(ValueError, match="unqualified"):
            j.prepare_place(PlaceAction(OrderIntent(
                "pa:BTC", Side.BID, D("1"), D("1"), client_order_id=CLOID2,
            )))
        assert j.load_recovery().result(timeout=3).unresolved == ()


def test_closed_journal_rejects_new_actions(tmp_path):
    j = journal(tmp_path)
    j.close()
    with pytest.raises(JournalUnavailable, match="not available"):
        j.prepare_place(place())


class FakeGateway:
    """No actual SDK calls, but a durable record must exist before the send."""
    def __init__(self, manager, j, *, fail=False):
        self.manager = manager
        self.journal = j
        self.fail = fail
        self.sent = []
        self.thread_names = []

    def dispatch(self, action, snapshot_provider):
        self.thread_names.append(threading.current_thread().name)
        if isinstance(action, PlaceAction):
            # This is not just a queued write: recovery can read the COMMITTED row.
            rows = self.journal.load_recovery().result(timeout=3).unresolved
            assert any(r.client_order_id == action.intent.client_order_id for r in rows)
            if self.fail:
                raise TimeoutError("unknown exchange status")
            snapshot_provider()  # should be invoked after persistence barrier
            self.sent.append(action)
            return GatewayOutcome(action, True, True, "resting")
        self.sent.append(action)
        return GatewayOutcome(action, True, True, "cancel_accepted_not_terminal")


def dispatcher(tmp_path):
    j = journal(tmp_path)
    manager = OrderManager(dex="pas", account_id=ACCOUNT, session_id="sqlite-test")
    gateway = FakeGateway(manager, j)
    return j, gateway, DurableExecutionDispatcher(gateway=gateway, journal=j)


def test_async_dispatch_commits_before_gateway_and_not_on_caller_thread(tmp_path):
    j, gateway, execution = dispatcher(tmp_path)
    caller = threading.current_thread().name
    try:
        future = execution.submit(place(), lambda: object())
        assert isinstance(future, Future)
        outcome = future.result(timeout=5)
        assert outcome.sent and outcome.reason == "resting"
        assert gateway.thread_names[0] != caller
        assert j.load_recovery().result(timeout=3).unresolved[0].dispatch_state == "SENT"
    finally:
        execution.close()
        j.close()


def test_prepare_failure_prevents_sdk_and_triggers_recovery(tmp_path):
    j, gateway, execution = dispatcher(tmp_path)
    try:
        j.close()  # simulate unavailable disk/journal writer
        with pytest.raises(JournalUnavailable, match="NOT SENT"):
            execution.submit(place(), lambda: object()).result(timeout=5)
        assert gateway.sent == []
        assert not gateway.manager.ready
        assert gateway.manager.blocked_reason == "order_journal_prepare_failed"
    finally:
        execution.close()


def test_unknown_post_prepare_dispatch_is_recoverable(tmp_path):
    j, gateway, execution = dispatcher(tmp_path)
    gateway.fail = True
    try:
        with pytest.raises(TimeoutError, match="unknown"):
            execution.submit(place(), lambda: object()).result(timeout=5)
        assert gateway.manager.blocked_reason == "order_dispatch_uncertain"
        assert j.load_recovery().result(timeout=3).unresolved[0].dispatch_state == "PREPARED"
    finally:
        execution.close()
        j.close()


def test_emergency_cancel_does_not_wait_for_db(tmp_path):
    j, gateway, execution = dispatcher(tmp_path)
    j.close()  # even with journal unavailable, cancellation must be attempted
    try:
        cancel = CancelAction("BTC", CLOID, 15)
        with pytest.raises(JournalUnavailable, match="cancel attempted"):
            execution.submit(cancel, lambda: (_ for _ in ()).throw(
                AssertionError("no snapshot on cancel")
            )).result(timeout=5)
        assert gateway.sent == [cancel]
        assert gateway.manager.blocked_reason == "cancel_audit_failed"
    finally:
        execution.close()


def test_cancel_audit_persists_when_healthy(tmp_path):
    j, gateway, execution = dispatcher(tmp_path)
    try:
        cancel = CancelAction("BTC", CLOID, 15)
        assert execution.submit(cancel, lambda: None).result(timeout=5).sent
        with sqlite3.connect(j.path) as conn:
            row = conn.execute(
                "SELECT kind,client_order_id,dispatch_state FROM journal_actions "
                "WHERE kind='CANCEL'"
            ).fetchone()
        assert row == ("CANCEL", CLOID, "SENT")
    finally:
        execution.close()
        j.close()


def test_different_account_cannot_attach_to_dispatcher(tmp_path):
    with journal(tmp_path) as j:
        manager = OrderManager(dex="pas", account_id="0x" + "f" * 40)
        with pytest.raises(ValueError, match="mismatch"):
            DurableExecutionDispatcher(
                gateway=FakeGateway(manager, j), journal=j,
            )
