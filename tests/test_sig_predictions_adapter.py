from __future__ import annotations

from decimal import Decimal
import json

import pytest

from exchanges.sig_predictions.client import SigApiError, SigPredictionsVenueClient
from exchanges.sig_predictions.config import SigPredictionsVenueConfig
from exchanges.sig_predictions.types import (
    OrderAction,
    OutcomeSide,
    SigBookLevel,
    SigMarketSpec,
    SigOrderBookSnapshot,
    SigOrderRequest,
    SigOrderResult,
)
from market_handler.sig_predictions_adapter import SigPredictionsMarketAdapter
from model.order import ExecutionStatus, OrderIntent, Side


def test_snapshot_is_yes_normalized_and_uses_exchange_as_instrument() -> None:
    adapter = SigPredictionsMarketAdapter()
    state = adapter.snapshot(
        SigOrderBookSnapshot(
            exchange_id="36",
            market_id="26",
            timestamp_ms=123,
            bids=(SigBookLevel("0.4", "3"), SigBookLevel("0.5", "2")),
            asks=(SigBookLevel("0.7", "1"), SigBookLevel("0.6", "4")),
            last_trade_price="0.55",
        )
    )

    assert state.instrument == "SIG_EXCHANGE:36"
    assert state.best_bid == Decimal("0.5")
    assert state.best_ask == Decimal("0.6")
    assert state.last_trade_price == Decimal("0.55")
    assert state.is_valid


def test_bid_maps_to_buy_yes() -> None:
    adapter = SigPredictionsMarketAdapter()
    request = adapter.to_venue_order(
        OrderIntent(
            instrument="SIG_EXCHANGE:36",
            side=Side.BID,
            price=Decimal("0.420"),
            size=Decimal("100"),
            post_only=False,
            client_order_id="bid-1",
        ),
        SigMarketSpec(
            market_id="26",
            exchange_id="36",
            tournament_id="t1",
        ),
    )

    assert request.exchange_id == "36"
    assert request.outcome is OutcomeSide.YES
    assert request.action is OrderAction.BUY
    assert request.price == 0.42
    assert request.quantity == 100
    assert request.idempotency_key == "bid-1"
    assert request.tournament_id == "t1"


def test_ask_maps_to_complement_buy_no() -> None:
    adapter = SigPredictionsMarketAdapter()
    request = adapter.to_venue_order(
        OrderIntent(
            instrument="SIG_EXCHANGE:36",
            side=Side.ASK,
            price=Decimal("0.630"),
            size=Decimal("50"),
            post_only=False,
            client_order_id="ask-1",
        ),
        SigMarketSpec(market_id="26", exchange_id="36"),
    )

    assert request.outcome is OutcomeSide.NO
    assert request.action is OrderAction.BUY
    assert request.price == 0.37
    assert request.quantity == 50


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("1"), Decimal("0.421")])
def test_rejects_non_limit_or_off_tick_price(price: Decimal) -> None:
    adapter = SigPredictionsMarketAdapter()
    with pytest.raises(ValueError):
        adapter.to_venue_order(
            OrderIntent(
                instrument="SIG_EXCHANGE:36",
                side=Side.BID,
                price=price,
                size=Decimal("1"),
                post_only=False,
                client_order_id="x",
            ),
            SigMarketSpec(market_id="26", exchange_id="36"),
        )


def test_rejects_fractional_quantity() -> None:
    adapter = SigPredictionsMarketAdapter()
    with pytest.raises(ValueError):
        adapter.to_venue_order(
            OrderIntent(
                instrument="SIG_EXCHANGE:36",
                side=Side.BID,
                price=Decimal("0.42"),
                size=Decimal("1.5"),
                post_only=False,
                client_order_id="x",
            ),
            SigMarketSpec(market_id="26", exchange_id="36"),
        )


def test_rejects_post_only_because_api_has_no_post_only_field() -> None:
    adapter = SigPredictionsMarketAdapter()
    with pytest.raises(ValueError, match="post-only"):
        adapter.to_venue_order(
            OrderIntent(
                instrument="SIG_EXCHANGE:36",
                side=Side.BID,
                price=Decimal("0.42"),
                size=Decimal("1"),
                post_only=True,
                client_order_id="x",
            ),
            SigMarketSpec(market_id="26", exchange_id="36"),
        )


def test_rejects_missing_idempotency_key() -> None:
    adapter = SigPredictionsMarketAdapter()
    with pytest.raises(ValueError, match="idempotency"):
        adapter.to_venue_order(
            OrderIntent(
                instrument="SIG_EXCHANGE:36",
                side=Side.BID,
                price=Decimal("0.42"),
                size=Decimal("1"),
                post_only=False,
            ),
            SigMarketSpec(market_id="26", exchange_id="36"),
        )


def test_execution_result_preserves_accepted_noop() -> None:
    adapter = SigPredictionsMarketAdapter()
    report = adapter.to_internal_execution_report(
        SigOrderResult(
            order_id=None,
            exchange_id="36",
            open=False,
            quantity_traded="0",
            total_cost="0",
            fill_price=None,
        )
    )

    assert report.status is ExecutionStatus.NOOP
    assert report.exchange_order_id is None
    assert report.filled_size == Decimal("0")
    assert report.average_price is None


def test_execution_result_detects_partial_then_resting() -> None:
    adapter = SigPredictionsMarketAdapter()
    report = adapter.to_internal_execution_report(
        SigOrderResult(
            order_id=1001,
            exchange_id="36",
            open=True,
            quantity_traded="25",
            total_cost="10.5",
            fill_price="0.42",
            remaining_quantity="75",
            quantity=100,
        )
    )

    assert report.status is ExecutionStatus.PARTIALLY_FILLED
    assert report.exchange_order_id == 1001
    assert report.filled_size == Decimal("25")
    # POST /orders exposes last fill, not lifecycle average.
    assert report.average_price is None


def test_sdk_serializes_single_order_and_auth_without_retry(monkeypatch):
    sdk_module = pytest.importorskip("super_market")
    import httpx
    monkeypatch.setenv("SIG_PREDICTIONS_API_KEY", "secret")
    requests = []

    def handler(req):
        requests.append(req)
        if req.method == "POST" and req.url.path == "/api/v1/orders":
            return httpx.Response(200, json={
                "orderId": 1001, "exchangeId": "36", "open": True,
                "remainingQuantity": 100, "action": "buy", "side": "yes",
                "price": 0.42, "quantity": 100,
                "quantityTraded": 0, "totalCost": 0, "fillPrice": None,
            })
        raise AssertionError(f"unexpected network request: {req.method} {req.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    sdk = sdk_module.SuperMarketAPI(
        bearer_auth="secret", max_retries=0, http_client=http_client,
        base_url="https://www.thesuper.market/api/v1",
    )
    try:
        client = SigPredictionsVenueClient(sdk=sdk, allow_trading=True)
        result = client.place_order(SigOrderRequest(
            exchange_id="36", outcome=OutcomeSide.YES, action=OrderAction.BUY,
            quantity=100, price=0.42, idempotency_key="order-1", tournament_id="t1",
        ))
        assert len(requests) == 1
        req = requests[0]
        assert req.method == "POST"
        assert req.headers["Authorization"] == "Bearer secret"
        assert json.loads(req.content) == {
            "exchangeId": "36", "side": "yes", "action": "buy",
            "quantity": 100, "price": 0.42,
            "idempotencyKey": "order-1", "tournamentId": "t1",
        }
        assert result.order_id == 1001
        assert result.open
    finally:
        sdk.close()
        http_client.close()


def test_sdk_orders_pagination_cancel_and_fills_on_mock_transport():
    sdk_module = pytest.importorskip("super_market")
    import httpx
    routes = []
    order = {
        "id": 1001, "exchangeId": "36", "side": "yes", "action": "buy",
        "quantity": 1, "priceLimit": 0.005, "open": False,
        "createdAt": "2026-10-21T00:00:00Z",
    }

    def handler(req):
        routes.append((req.method, req.url.path, dict(req.url.params)))
        path, method = req.url.path, req.method
        if path == "/api/v1/orders" and method == "GET":
            return httpx.Response(200, json={
                "data": [order], "pagination": {"limit": 50, "hasMore": True, "nextCursor": "page2"},
            })
        if path == "/api/v1/orders/1001" and method == "GET":
            return httpx.Response(200, json=order)
        if path == "/api/v1/orders/1001" and method == "DELETE":
            return httpx.Response(200, json={"orderId": 1001, "message": "Cancelled"})
        if path == "/api/v1/orders/1001/fills" and method == "GET":
            return httpx.Response(200, json={
                "orderId": 1001, "exchangeId": "36", "data": [{
                    "id": 55, "price": 0.005, "quantity": 1, "side": "yes",
                    "filledAt": "2026-10-21T00:00:01Z",
                }],
                "pagination": {"limit": 50, "hasMore": False, "nextCursor": None},
                "coverage": {"complete": True},
                "totalQuantityFilled": 1, "avgFillPrice": 0.005,
            })
        raise AssertionError(f"unexpected request: {method} {path}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    sdk = sdk_module.SuperMarketAPI(
        bearer_auth="test-key", max_retries=0, http_client=http_client,
        base_url="https://www.thesuper.market/api/v1",
    )
    try:
        client = SigPredictionsVenueClient(sdk=sdk, allow_trading=True)
        listed = client.list_orders(status="open", exchange_id="36", tournament_id="t1")
        assert listed["orders"][0]["id"] == 1001
        assert listed["nextCursor"] == "page2"
        assert listed["hasMore"] is True
        assert client.get_order(1001)["open"] is False
        fills = client.get_order_fills(1001)
        assert len(fills["fills"]) == 1
        assert fills["totalQuantityFilled"] == 1
        assert fills["hasMore"] is False
        assert client.cancel_order(1001)["orderId"] == 1001
        assert routes[0][0:2] == ("GET", "/api/v1/orders")
        assert routes[-1][0:2] == ("DELETE", "/api/v1/orders/1001")
    finally:
        sdk.close()
        http_client.close()


def test_realtime_channel_and_retry_classification(monkeypatch):
    monkeypatch.setenv("SIG_PREDICTIONS_TOURNAMENT_ID", "t1")
    sdk = type("DummySDK", (), {"max_retries": 0})()
    client = SigPredictionsVenueClient(SigPredictionsVenueConfig(), sdk=sdk)
    assert client.realtime_market_channel("26") == "tournament:t1:market:26"
    assert client.should_retry_cancel(503)
    assert SigApiError(status=429, code="RATE_LIMITED", message="slow down").retryable
    assert SigApiError(status=409, code="REQUEST_IN_FLIGHT", message="still executing").retryable
    assert not SigApiError(status=400, code="VALIDATION_ERROR", message="bad input").retryable


def test_client_is_readonly_by_default_even_when_api_key_is_present(monkeypatch):
    monkeypatch.setenv("SIG_PREDICTIONS_API_KEY", "test-only")
    called = []
    class NeverCall:
        max_retries = 0
        class Orders:
            def __getattr__(self, _name):
                def hit(*args, **kwargs):
                    called.append((args, kwargs))
                    raise AssertionError("network should not be called")
                return hit
        orders = Orders()
    client = SigPredictionsVenueClient(sdk=NeverCall())
    req = SigOrderRequest(
        exchange_id="36", outcome=OutcomeSide.YES, action=OrderAction.BUY,
        quantity=1, price=0.5, idempotency_key="unique-key",
    )
    with pytest.raises(PermissionError):
        client.place_order(req)
    with pytest.raises(PermissionError):
        client.place_multi_leg([req], idempotency_key="multi")
    with pytest.raises(PermissionError):
        client.place_batch([req], idempotency_key="batch")
    with pytest.raises(PermissionError):
        client.cancel_order(123)
    with pytest.raises(PermissionError):
        client.cancel_all_orders(exchange_id="36")
    assert called == []


def test_idempotency_expiry_is_not_retryable():
    error = SigApiError(status=409, code="IDEMPOTENCY_KEY_EXPIRED", message="expired")
    assert not error.retryable
