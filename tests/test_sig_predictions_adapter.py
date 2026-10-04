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


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_client_serializes_exact_single_order_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIG_PREDICTIONS_API_KEY", "secret")
    captured: dict[str, object] = {}

    def opener(request: object, *, timeout: float) -> _FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeResponse(
            {
                "orderId": 1001,
                "exchangeId": "36",
                "open": True,
                "remainingQuantity": 100,
                "action": "buy",
                "side": "yes",
                "price": 0.42,
                "quantity": 100,
                "terminalReasonCode": None,
                "quantityTraded": 0,
                "totalCost": 0,
                "fillPrice": None,
                "all": None,
            }
        )

    client = SigPredictionsVenueClient(opener=opener)
    result = client.place_order(
        SigOrderRequest(
            exchange_id="36",
            outcome=OutcomeSide.YES,
            action=OrderAction.BUY,
            quantity=100,
            price=0.42,
            idempotency_key="order-1",
            tournament_id="t1",
        )
    )

    request = captured["request"]
    assert request.full_url == "https://www.thesuper.market/api/v1/orders"
    assert request.get_header("Authorization") == "Bearer secret"
    assert json.loads(request.data) == {
        "idempotencyKey": "order-1",
        "exchangeId": "36",
        "side": "yes",
        "action": "buy",
        "quantity": 100,
        "price": 0.42,
        "tournamentId": "t1",
    }
    assert result.order_id == 1001
    assert result.open


def test_realtime_channel_and_retry_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIG_PREDICTIONS_TOURNAMENT_ID", "t1")
    client = SigPredictionsVenueClient(SigPredictionsVenueConfig())

    assert client.realtime_market_channel("26") == "tournament:t1:market:26"
    assert client.should_retry_cancel(503)

    assert SigApiError(
        status=429,
        code="RATE_LIMITED",
        message="slow down",
    ).retryable
    assert SigApiError(
        status=409,
        code="REQUEST_IN_FLIGHT",
        message="still executing",
    ).retryable
    assert not SigApiError(
        status=400,
        code="VALIDATION_ERROR",
        message="bad input",
    ).retryable
