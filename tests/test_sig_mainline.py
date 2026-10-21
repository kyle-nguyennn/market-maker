"""SIG mainline lifecycle and read-only smoke tests: no external API calls."""
from decimal import Decimal as D
from datetime import UTC, datetime
import json

import pytest

from apps.sig_predictions import build_stack, check_account
from exchanges.sig_predictions.client import SigPredictionsVenueClient
from exchanges.sig_predictions.types import SigMarketSpec
from execution.order_manager import OrderManager, OrderState
from market_handler.sig_predictions_adapter import SigPredictionsMarketAdapter
from model.order import OrderEvent, OrderEventType as E, OrderIntent, Side


def test_sig_internal_instrument_reuses_shared_order_manager():
    mm = OrderManager(session_id="sig-run", dex="sig:tournament-1", account_id="acct-1")
    assert not mm.ready
    assert mm.resynchronize([], positions_reconciled=True, fills_reconciled=True)
    intent = OrderIntent("SIG_EXCHANGE:36", Side.BID, D("0.42"),
                         D("100"), post_only=False)
    actions = mm.reconcile([intent])
    assert len(actions) == 1
    allocated = actions[0].intent
    assert allocated.client_order_id.startswith("0x")
    assert len(allocated.client_order_id) == 34
    adapter = SigPredictionsMarketAdapter()
    request = adapter.to_venue_order(
        allocated, SigMarketSpec(market_id="26", exchange_id="36",
                                  tournament_id="tournament-1")
    )
    assert request.idempotency_key == allocated.client_order_id
    assert request.quantity == 100
    assert request.price == 0.42
    assert mm.reconcile([intent]) == []
    mm.on_order_update(OrderEvent(
        allocated.client_order_id, E.RESTING, exchange_order_id=123,
        instrument="SIG_EXCHANGE:36", dex="sig:tournament-1", account_id="acct-1",
    ))
    assert mm.live_orders[allocated.client_order_id].state is OrderState.RESTING
    cancels = mm.reconcile([])
    assert len(cancels) == 1
    assert mm.reconcile([intent]) == []
    mm.on_order_update(OrderEvent(
        allocated.client_order_id, E.CANCEL_ACCEPTED, exchange_order_id=123,
        instrument="SIG_EXCHANGE:36", dex="sig:tournament-1", account_id="acct-1",
    ))
    assert mm.live_orders[allocated.client_order_id].state is OrderState.PENDING_CANCEL


def test_sig_order_updates_reject_other_tournament():
    mm = OrderManager(session_id="sig-run", dex="sig:tournament-1", account_id="acct-1")
    assert mm.resynchronize([], positions_reconciled=True, fills_reconciled=True)
    intent = OrderIntent("SIG_EXCHANGE:36", Side.ASK, D("0.63"),
                         D("10"), post_only=False)
    cloid = mm.reconcile([intent])[0].intent.client_order_id
    with pytest.raises(ValueError, match="cross-dex"):
        mm.on_order_update(OrderEvent(
            cloid, E.RESTING, exchange_order_id=1,
            dex="sig:another-tournament", account_id="acct-1",
        ))
    assert mm.live_orders[cloid].state is OrderState.PENDING_NEW


class FakeResponse:
    def __init__(self, payload):
        self.value = json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return None
    def read(self):
        return self.value


def test_sig_readonly_cli_account_and_book(monkeypatch, capsys):
    monkeypatch.setenv("SIG_PREDICTIONS_API_KEY", "test-only")
    seen = []
    def opener(request, *, timeout):
        assert request.get_header("Authorization") == "Bearer test-only"
        assert request.get_method() == "GET"
        seen.append(request.full_url)
        if "/orderbook" in request.full_url:
            return FakeResponse({
                "exchangeId": "36", "marketId": "26",
                "asOf": {"sequence": 5, "at": "2026-10-21T00:00:00Z"},
                "bids": [{"price": 0.4, "quantity": 12}],
                "asks": [{"price": 0.6, "quantity": 4}],
            })
        if request.full_url.endswith("/markets?limit=5&sort=recent&status=any"):
            return FakeResponse({"markets": [{"id": "26"}]})
        return FakeResponse({})
    client = SigPredictionsVenueClient(opener=opener)
    check_account(client, exchange_id="36")
    output = capsys.readouterr().out
    assert "trading" not in output.lower() or "no orders" in output.lower()
    assert "Book SIG_EXCHANGE:36" in output
    assert "Markets: 1 returned" in output
    assert len(seen) == 4
    assert all("/api/v1/" in url for url in seen)
    assert all("test-only" not in url for url in seen)


def test_sig_readonly_cli_rejects_invalid_exchange_and_missing_key(monkeypatch):
    monkeypatch.delenv("SIG_PREDICTIONS_API_KEY", raising=False)
    client = SigPredictionsVenueClient(opener=lambda *_1, **_2: None)
    with pytest.raises(ValueError, match="SIG_PREDICTIONS_API_KEY"):
        check_account(client)
    monkeypatch.setenv("SIG_PREDICTIONS_API_KEY", "test-only")
    with pytest.raises(ValueError, match="positive"):
        check_account(client, exchange_id="../42")
