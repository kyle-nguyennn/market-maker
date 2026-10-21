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


from types import SimpleNamespace


class StubResource:
    def __init__(self, **responses):
        self.responses = responses
        self.calls = []

    def __getattr__(self, name):
        if name not in self.responses:
            raise AttributeError(name)

        def invoke(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self.responses[name]
        return invoke


def fake_sdk():
    account = StubResource(list={})
    portfolio = StubResource(list_positions={"positions": []})
    markets = StubResource(list={"data": [{"id": "26"}], "pagination": {"nextCursor": None}})
    tournaments = StubResource(retrieve={"id": "12345678-1234-1234-1234-123456789abc"})
    scoped = StubResource(list_positions={"positions": []})
    exchanges = StubResource(list_orderbook={
        "exchangeId": "36", "marketId": "26",
        "asOf": {"sequence": 5, "at": "2026-10-21T00:00:00Z"},
        "bids": [{"price": 0.4, "quantity": 12}],
        "asks": [{"price": 0.6, "quantity": 4}],
    })
    sdk = SimpleNamespace(
        max_retries=0, account=account, portfolio=portfolio, markets=markets,
        tournaments=SimpleNamespace(retrieve=tournaments.retrieve, portfolio=scoped),
        exchanges=exchanges, close=lambda: None,
    )
    return sdk, account, portfolio, markets, tournaments, exchanges


def test_sig_readonly_cli_account_and_book(monkeypatch, capsys):
    monkeypatch.setenv("SIG_PREDICTIONS_API_KEY", "test-only")
    sdk, account, portfolio, markets, _, exchanges = fake_sdk()
    client = SigPredictionsVenueClient(sdk=sdk)
    check_account(client, exchange_id="36")
    output = capsys.readouterr().out
    assert "Book SIG_EXCHANGE:36" in output
    assert "Markets: 1 returned" in output
    assert len(account.calls) == 1
    assert len(portfolio.calls) == 1
    assert markets.calls[0][2]["limit"] == 5
    assert exchanges.calls[0][2]["id"] == "36"


def test_sig_readonly_cli_rejects_invalid_exchange_and_missing_key(monkeypatch):
    monkeypatch.delenv("SIG_PREDICTIONS_API_KEY", raising=False)
    sdk, *_ = fake_sdk()
    client = SigPredictionsVenueClient(sdk=sdk)
    with pytest.raises(ValueError, match="SIG_PREDICTIONS_API_KEY"):
        check_account(client)
    monkeypatch.setenv("SIG_PREDICTIONS_API_KEY", "test-only")
    with pytest.raises(ValueError, match="positive"):
        check_account(client, exchange_id="../42")


def test_sig_readonly_check_prints_resolved_tournament_uuid(monkeypatch, capsys):
    monkeypatch.setenv("SIG_PREDICTIONS_API_KEY", "test-only")
    sdk, *_ = fake_sdk()
    client = SigPredictionsVenueClient(sdk=sdk)
    check_account(client, tournament_slug="midterm-elections")
    assert "12345678-1234-1234-1234-123456789abc" in capsys.readouterr().out
