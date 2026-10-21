"""Offline manual CLI tests: discovery, guarded writes, order fills and Phoenix WS."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from apps import sig_cli, sig_realtime_stream as ws
from exchanges.sig_predictions.types import (
    SigBookLevel, SigBookVersion, SigOrderBookSnapshot, SigOrderResult,
)


TID = "12345678-1234-1234-1234-123456789abc"


class FakeTradingClient:
    allow_trading = True

    def __init__(self):
        self.placed = []
        self.cancelled = []
        self.reads = []

    def get_order_book(self, eid, *, tournament_id, depth):
        assert eid == "36"
        assert tournament_id == TID
        return SigOrderBookSnapshot(
            exchange_id=eid, market_id="26", timestamp_ms=int(datetime.now(UTC).timestamp() * 1000),
            bids=(SigBookLevel("0.4", "2"),), asks=(SigBookLevel("0.6", "3"),),
            as_of=SigBookVersion(13, datetime.now(UTC).isoformat()),
        )

    def place_order(self, request):
        self.placed.append(request)
        return SigOrderResult(order_id=42, exchange_id="36", open=True,
                              quantity_traded="0", total_cost="0", fill_price=None)

    def get_order(self, oid):
        self.reads.append(oid)
        return {"id": 42, "open": False, "tournamentId": TID, "exchangeId": "36"}

    def get_order_fills(self, oid, limit=50):
        return {"fills": [{"id": 8, "quantity": 1}], "hasMore": False,
                "totalQuantityFilled": 1, "avgFillPrice": 0.005}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return {"orderId": 42, "message": "Cancelled"}


def test_parser_ids_tick_and_manual_guards():
    with pytest.raises(ValueError):
        sig_cli.numeric_id("../36")
    with pytest.raises(ValueError):
        sig_cli.tournament_id("midterm-elections")
    assert sig_cli.tournament_id(TID) == TID
    assert sig_cli.limit_price("0.005") == 0.005
    for value in ("0", "1", "0.003", "NaN", "Infinity", "0.001"):
        with pytest.raises(ValueError):
            sig_cli.limit_price(value)
    a = sig_cli.args_parser(["tournaments"])
    assert a.status == "active"
    assert sig_cli.args_parser(["place", "--exchange-id", "36", "--tournament-id", TID,
                                "--side", "yes", "--action", "buy", "--quantity", "1",
                                "--price", "0.005"]).live is False


def test_place_dry_run_never_writes(tmp_path, capsys):
    args = sig_cli.args_parser(["place", "--exchange-id", "36", "--tournament-id", TID,
                                "--side", "yes", "--action", "buy", "--quantity", "1",
                                "--price", "0.005", "--state-dir", str(tmp_path)])
    fake = FakeTradingClient()
    sig_cli.place(fake, args)
    assert fake.placed == []
    assert list(tmp_path.iterdir()) == []
    assert "dry_run" in capsys.readouterr().out


def test_place_live_requires_exact_confirmations_and_persists_before_post(tmp_path):
    args = sig_cli.args_parser(["place", "--exchange-id", "36", "--tournament-id", TID,
                                "--side", "no", "--action", "buy", "--quantity", "1",
                                "--price", "0.395", "--state-dir", str(tmp_path),
                                "--live", "--accept-fill-risk"])
    fake = FakeTradingClient()
    with pytest.raises(PermissionError):
        sig_cli.place(fake, args)
    assert not fake.placed
    assert list(tmp_path.iterdir()) == []
    args.confirm_exchange_id = "36"
    args.confirm_tournament_id = TID

    original = fake.place_order
    def check_prepared(order):
        states = list(tmp_path.glob("*.json"))
        assert len(states) == 1
        state = json.loads(states[0].read_text())
        assert state["phase"] == "PREPARED"
        assert state["idempotency_key"] == order.idempotency_key
        assert order.tournament_id == TID
        return original(order)

    fake.place_order = check_prepared
    sig_cli.place(fake, args)
    assert len(fake.placed) == 1
    state = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert state["phase"] == "ACKED"
    assert state["order_id"] == 42


def test_place_ambiguous_ack_does_not_retry_and_journal_survives(tmp_path):
    args = sig_cli.args_parser(["place", "--exchange-id", "36", "--tournament-id", TID,
                                "--side", "yes", "--action", "buy", "--quantity", "2",
                                "--price", "0.005", "--state-dir", str(tmp_path),
                                "--live", "--confirm-exchange-id", "36",
                                "--confirm-tournament-id", TID, "--accept-fill-risk"])
    fake = FakeTradingClient()
    n = []
    def fail_once(order):
        n.append(order)
        raise TimeoutError("unknown whether server accepted")
    fake.place_order = fail_once
    with pytest.raises(TimeoutError):
        sig_cli.place(fake, args)
    assert len(n) == 1
    state = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert state["phase"] == "PREPARED"
    assert state["idempotency_key"] == n[0].idempotency_key


def test_cancel_requires_tournament_and_order_confirmation(capsys):
    args = sig_cli.args_parser(["cancel", "42", "--tournament-id", TID])
    fake = FakeTradingClient()
    sig_cli.cancel(fake, args)
    assert fake.cancelled == []
    args.live = True
    with pytest.raises(PermissionError):
        sig_cli.cancel(fake, args)
    args.confirm_order_id = "42"
    sig_cli.cancel(fake, args)
    assert fake.cancelled == ["42"]


def test_watch_order_reports_full_order_quantity_not_only_current_page(capsys):
    args = sig_cli.args_parser(["watch-order", "42", "--polls", "3"])
    sig_cli.watch_order(FakeTradingClient(), args)
    output = capsys.readouterr().out
    assert "terminal_observed" in output
    assert '"totalQuantityFilled": 1' in output


def test_socket_url_and_channel_selection_do_not_mix_market_with_exchange():
    url = ws.socket_url("https://example.supabase.co", "public-api-key")
    assert url.startswith("wss://example.supabase.co/realtime/v1/websocket?")
    assert "vsn=2.0.0" in url
    with pytest.raises(ValueError):
        ws.socket_url("http://example.supabase.co", "key")
    with pytest.raises(ValueError):
        ws.socket_url("https://example.supabase.co/evil", "key")
    args = SimpleNamespace(tournament_id=TID, market_id="26", all_markets=False, user=False)
    assert ws.channel_name(args, {}) == f"tournament:{TID}:market:26"
    args.all_markets = True
    assert ws.channel_name(args, {}) == f"tournament:{TID}:markets"
    args.user = True
    assert ws.channel_name(args, {"channels": {"user": "user:test"}}) == "user:test"


def test_phoenix_frame_rejects_invalid_shapes_and_redacts_secrets():
    assert ws.parse_phoenix_message('["1","2","realtime:my-room","broadcast",{"x":1}]') == (
        "1", "2", "realtime:my-room", {"x": 1}, "broadcast",
    )
    with pytest.raises(ValueError):
        ws.parse_phoenix_message('{"event": "broadcast"}')
    assert ws.safe_event({"access_token": "secret", "nested": {"anonKey": "api-key"}}) == {
        "access_token": "[REDACTED]", "nested": {"anonKey": "[REDACTED]"},
    }


def test_phoenix_stream_sends_join_and_prints_only_events(monkeypatch, capsys):
    pytest.importorskip("websockets", reason="SIG-only realtime dependency not installed in core/Hyperliquid")
    topic = f"tournament:{TID}:markets"
    frames = [
        json.dumps(["1", "1", "realtime:" + topic, "phx_reply",
                    {"status": "ok", "response": {}}]),
        json.dumps(["1", "2", "realtime:" + topic, "broadcast",
                    {"event": "markets_batch", "payload": {"marketId": "26",
                     "access_token": "DO_NOT_LOG"}}]),
    ]

    class FakeSocket:
        def __init__(self):
            self.sent = []
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return None
        async def send(self, v):
            self.sent.append(json.loads(v))
        async def recv(self):
            return frames.pop(0)

    fake_socket = FakeSocket()
    import websockets.asyncio.client as wsclient
    monkeypatch.setattr(wsclient, "connect", lambda *args, **kwargs: fake_socket)
    fake_client = SimpleNamespace(mint_realtime_token=lambda: {
        "token": "JWT_DO_NOT_LOG", "supabaseUrl": "https://project.supabase.co",
        "anonKey": "ANON_DO_NOT_LOG", "channels": {"user": "my-user-channel"},
    })
    args = SimpleNamespace(tournament_id=TID, market_id=None, all_markets=True,
                           user=False, seconds=10, max_events=1, reconnects=0)
    asyncio.run(ws.stream(fake_client, args))
    out = capsys.readouterr().out
    assert "subscribed" in out
    assert "markets_batch" in out
    assert "DO_NOT_LOG" not in out
    assert fake_socket.sent[0][3] == "phx_join"
    assert fake_socket.sent[0][4]["access_token"] == "JWT_DO_NOT_LOG"
    assert fake_socket.sent[0][2] == "realtime:" + topic
