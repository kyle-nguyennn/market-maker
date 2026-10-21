"""Deterministic SIG smoke tests. No authentic credentials or external POST."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from json import loads
from pathlib import Path

import pytest

from apps.sig_order_smoke import (
    SmokeUncertain, _terminal_evidence, _write_state, smoke,
)
from exchanges.sig_predictions.types import (
    SigBookLevel, SigBookVersion, SigOrderBookSnapshot, SigOrderResult,
    OutcomeSide, OrderAction,
)


TOURNAMENT = "12345678-1234-1234-1234-123456789abc"


class FakeClient:
    def __init__(self, *, live=False, path: Path | None = None):
        self.allow_trading = live
        self.path = path
        self.posts = []
        self.cancels = []
        self.status_reads = []
        self.book_age = 1
        self.book_bid = "0.4"
        self.book_ask = "0.6"
        self.existing_orders = []
        self.order_open = True
        self.post_error = None
        self.cancel_error = None
        self.get_order_payload = None

    def get_account(self):
        return {"id": "test-identity"}

    def get_order_book(self, exchange_id, *, tournament_id, depth=200):
        assert tournament_id == TOURNAMENT
        ts = datetime.fromtimestamp(datetime.now(UTC).timestamp() - self.book_age, UTC)
        return SigOrderBookSnapshot(
            exchange_id=str(exchange_id), market_id="26",
            timestamp_ms=int(ts.timestamp() * 1000),
            bids=(SigBookLevel(self.book_bid, "10"),),
            asks=(SigBookLevel(self.book_ask, "10"),),
            as_of=SigBookVersion(10, ts.isoformat()),
        )

    def list_orders(self, **kwargs):
        assert kwargs["status"] == "open"
        assert kwargs["exchange_id"] == "36"
        assert kwargs["tournament_id"] == TOURNAMENT
        return {"orders": list(self.existing_orders), "nextCursor": None}

    def place_order(self, order):
        assert self.allow_trading
        assert self.path is not None and self.path.exists(), "journal must exist before POST"
        saved = loads(self.path.read_text())
        assert saved["phase"] == "PREPARED"
        assert saved["idempotency_key"] == order.idempotency_key
        assert order.exchange_id == "36"
        assert order.outcome is OutcomeSide.YES
        assert order.action is OrderAction.BUY
        assert order.quantity == 1
        assert order.price == 0.005
        assert order.expiration_date.endswith("Z")
        self.posts.append(order)
        if self.post_error:
            raise self.post_error
        return SigOrderResult(
            order_id=42, exchange_id="36", open=True,
            quantity_traded="0", total_cost="0",
            fill_price=None, remaining_quantity="1", quantity=1,
        )

    def get_order(self, oid):
        assert str(oid) == "42"
        self.status_reads.append(oid)
        if self.get_order_payload is not None:
            return self.get_order_payload
        return {"order": {"status": "open" if self.order_open else "canceled"}}

    def cancel_order(self, oid):
        assert self.allow_trading
        assert str(oid) == "42"
        self.cancels.append(oid)
        if self.cancel_error:
            raise self.cancel_error
        self.order_open = False
        return {"cancelled": 1}


def test_dry_run_reads_book_and_account_but_never_writes(tmp_path, capsys):
    path = tmp_path / "smoke.json"
    client = FakeClient(live=False, path=path)
    smoke(client, exchange_id="36", tournament_id=TOURNAMENT,
          state_path=path, live=False)
    assert client.posts == []
    assert client.cancels == []
    assert not path.exists()
    assert "DRY RUN" in capsys.readouterr().out


def test_real_smoke_places_once_with_durable_key_then_targeted_cancel(tmp_path):
    path = tmp_path / "smoke.json"
    client = FakeClient(live=True, path=path)
    smoke(client, exchange_id="36", tournament_id=TOURNAMENT,
          state_path=path, live=True)
    data = loads(path.read_text())
    assert data["phase"] == "VERIFIED_TERMINAL"
    assert data["order_id"] == 42
    assert data["quantity"] == 1
    assert data["price"] == "0.005"
    assert len(client.posts) == 1
    assert client.cancels == [42]
    assert len(client.status_reads) >= 1


def test_second_run_is_blocked_even_after_success(tmp_path):
    path = tmp_path / "smoke.json"
    client = FakeClient(live=True, path=path)
    smoke(client, exchange_id="36", tournament_id=TOURNAMENT,
          state_path=path, live=True)
    with pytest.raises(FileExistsError):
        smoke(client, exchange_id="36", tournament_id=TOURNAMENT,
              state_path=path, live=True)
    assert len(client.posts) == 1


def test_ambiguous_post_has_no_blind_retry_and_can_not_recover_without_oid(tmp_path):
    path = tmp_path / "smoke.json"
    client = FakeClient(live=True, path=path)
    client.post_error = TimeoutError("POST timed out after possible submit")
    with pytest.raises(TimeoutError):
        smoke(client, exchange_id="36", tournament_id=TOURNAMENT,
              state_path=path, live=True)
    assert loads(path.read_text())["phase"] == "PREPARED"
    assert len(client.posts) == 1
    with pytest.raises(SmokeUncertain, match="order ID unknown"):
        smoke(client, exchange_id="36", tournament_id=TOURNAMENT,
              state_path=path, live=True, recover=True)
    assert len(client.posts) == 1
    assert not client.cancels


def test_cancel_failure_retains_order_id_for_safe_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr("apps.sig_order_smoke.time.sleep", lambda *_: None)
    path = tmp_path / "smoke.json"
    client = FakeClient(live=True, path=path)
    client.cancel_error = TimeoutError("cancel ACK lost")
    with pytest.raises(TimeoutError):
        smoke(client, exchange_id="36", tournament_id=TOURNAMENT,
              state_path=path, live=True)
    assert loads(path.read_text())["phase"] == "ACKED"
    assert len(client.posts) == 1
    client.cancel_error = None
    smoke(client, exchange_id="36", tournament_id=TOURNAMENT,
          state_path=path, live=True, recover=True)
    assert loads(path.read_text())["phase"] == "VERIFIED_TERMINAL"
    assert len(client.posts) == 1
    assert client.cancels == [42, 42]


def test_scope_checks_and_non_crossing_book_fail_closed(tmp_path):
    client = FakeClient(live=True, path=tmp_path / "s.json")
    client.book_bid = "0.005"
    client.book_ask = "0.01"
    with pytest.raises(SmokeUncertain, match="best ask too close"):
        smoke(client, exchange_id="36", tournament_id=TOURNAMENT,
              state_path=client.path, live=True)
    client.book_bid = "0.4"
    client.book_ask = "0.6"
    client.book_age = 900
    with pytest.raises(SmokeUncertain, match="too old"):
        smoke(client, exchange_id="36", tournament_id=TOURNAMENT,
              state_path=client.path, live=True)
    client.book_age = 1
    client.existing_orders = [{"orderId": 123}]
    with pytest.raises(SmokeUncertain, match="existing open orders"):
        smoke(client, exchange_id="36", tournament_id=TOURNAMENT,
              state_path=client.path, live=True)
    assert client.posts == []
    assert not client.path.exists()


def test_recovery_refuses_cross_tournament_file(tmp_path):
    path = tmp_path / "other.json"
    _write_state(path, {"exchange_id":"36",
                         "tournament_id":"11111111-1111-1111-1111-111111111111",
                         "order_id":42, "phase":"ACKED"}, initial=True)
    client = FakeClient(live=True)
    with pytest.raises(SmokeUncertain, match="wrong exchange/tournament"):
        smoke(client, exchange_id="36", tournament_id=TOURNAMENT,
              state_path=path, live=True, recover=True)
    assert client.cancels == []


def test_terminal_status_validation():
    assert _terminal_evidence({"order":{"status":"cancelled"}})
    assert _terminal_evidence({"open": False})
    assert not _terminal_evidence({"status": "open", "open": False})
    assert not _terminal_evidence({"status":"pending"})
    assert not _terminal_evidence({})


def test_dry_run_does_not_create_signing_client():
    from apps.sig_order_smoke import main
    from unittest.mock import patch
    with patch("sys.argv", ["sig_order_smoke", "--exchange-id", "36",
            "--tournament-id", TOURNAMENT, "--live"]), pytest.raises(SystemExit):
        main()
