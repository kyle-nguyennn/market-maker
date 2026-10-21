"""Offline parity checks for Scalar SDK-backed read-only venue facade."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from apps.sig_predictions import check_account
from exchanges.sig_predictions.sdk_read_client import SigPredictionsSDKReadClient
from exchanges.sig_predictions.types import OrderAction, OutcomeSide, SigOrderRequest


class SpyResource:
    def __init__(self, method, result):
        self.method = method
        self.result = result
        self.calls = []

    def __getattr__(self, name):
        if name == self.method:
            def invoke(*args, **kwargs):
                self.calls.append((args, kwargs))
                return self.result
            return invoke
        raise AttributeError(name)


def fake_sdk():
    as_of = datetime.now(UTC).isoformat()
    account = SpyResource("list", {"balance": 500})
    markets = SpyResource("list", {"data": [{"id": "26"}], "pagination": {}})
    positions = SpyResource("list_positions", {"positions": []})
    tournament = SpyResource("retrieve", {"id": "12345678-1234-1234-1234-123456789abc"})
    scoped = SpyResource("list_positions", {"positions": [{"exchangeId": "36"}]})
    book = SpyResource("list_orderbook", {
        "exchangeId": "36", "marketId": "26",
        "asOf": {"sequence": 123, "at": as_of},
        "bids": [{"price": 0.4, "quantity": 3}],
        "asks": [{"price": 0.6, "quantity": 2}],
    })
    return SimpleNamespace(
        max_retries=0,
        account=account,
        markets=markets,
        portfolio=positions,
        tournaments=SimpleNamespace(retrieve=tournament.retrieve, portfolio=scoped),
        exchanges=book,
        orders=SimpleNamespace(create=lambda **_: (_ for _ in ()).throw(AssertionError("wrote"))),
        close=lambda: None,
    ), account, markets, positions, scoped, book


def test_sdk_client_normalizes_market_collection_and_book(capsys, monkeypatch):
    monkeypatch.setenv("SIG_PREDICTIONS_API_KEY", "test-only")
    sdk, account, markets, positions, _, book = fake_sdk()
    client = SigPredictionsSDKReadClient(sdk=sdk)
    check_account(client, exchange_id="36")
    captured = capsys.readouterr().out
    assert "Markets: 1 returned" in captured
    assert "bid=0.4, ask=0.6" in captured
    assert account.calls == [((), {})]
    assert positions.calls == [((), {})]
    assert markets.calls[0][1] == {"limit": 5, "sort": "recent", "status": "any"}
    assert book.calls[0][1]["id"] == "36"
    assert book.calls[0][1]["depth"] == 200


def test_sdk_readonly_tournament_is_scoped():
    sdk, _, _, _, scoped, _ = fake_sdk()
    client = SigPredictionsSDKReadClient(sdk=sdk)
    assert client.get_positions(tournament_slug="midterms") == {
        "positions": [{"exchangeId": "36"}],
    }
    assert scoped.calls[0][1] == {"slug": "midterms"}
    assert client.get_tournament("midterms")["id"].startswith("12345678")


def test_sdk_readonly_blocks_every_signed_operation():
    sdk, *_ = fake_sdk()
    client = SigPredictionsSDKReadClient(sdk=sdk)
    request = SigOrderRequest(
        exchange_id="36", outcome=OutcomeSide.YES,
        action=OrderAction.BUY, quantity=1, price=0.005,
        idempotency_key="once",
    )
    with pytest.raises(PermissionError):
        client.place_order(request)
    with pytest.raises(PermissionError):
        client.cancel_order(42)
    with pytest.raises(PermissionError):
        client.cancel_all_orders(exchange_id="36")
    with pytest.raises(PermissionError):
        client._request("POST", "/orders", body={"idempotencyKey": "once"})
    with pytest.raises(NotImplementedError):
        client.get_order(42)


def test_sdk_implicit_retries_are_forbidden():
    sdk, *_ = fake_sdk()
    sdk.max_retries = 2
    with pytest.raises(ValueError, match="max_retries=0"):
        SigPredictionsSDKReadClient(sdk=sdk)
