from decimal import Decimal

import pytest

from exchanges.sig_predictions.client import SigPredictionsVenueClient
from exchanges.sig_predictions.config import SigPredictionsVenueConfig
from exchanges.sig_predictions.types import (
    Outcome,
    SigBookLevel,
    SigMarketSpec,
    SigOrderBookSnapshot,
    SigOrderResult,
    VenueSide,
)
from market_handler.sig_predictions_adapter import SigPredictionsMarketAdapter
from model.order import ExecutionStatus, OrderIntent, Side


def test_snapshot_converts_to_decimal_and_sorts_book() -> None:
    adapter = SigPredictionsMarketAdapter()
    state = adapter.snapshot(
        SigOrderBookSnapshot(
            market_id="m1",
            outcome=Outcome.YES,
            timestamp_ms=123,
            bids=(SigBookLevel("0.4", "3"), SigBookLevel("0.5", "2")),
            asks=(SigBookLevel("0.7", "1"), SigBookLevel("0.6", "4")),
            as_of="42",
            last_trade_price="0.55",
        )
    )

    assert state.instrument == "SIG_PREDICTION:m1:YES"
    assert state.best_bid == Decimal("0.5")
    assert state.best_ask == Decimal("0.6")
    assert state.last_trade_price == Decimal("0.55")
    assert state.is_valid


def test_order_preserves_explicit_outcome_and_side() -> None:
    adapter = SigPredictionsMarketAdapter()
    request = adapter.to_venue_order(
        OrderIntent(
            instrument="SIG_PREDICTION:m1:NO",
            side=Side.ASK,
            price=Decimal("0.37"),
            size=Decimal("12.5"),
            post_only=False,
        ),
        SigMarketSpec(market_id="m1", outcome=Outcome.NO),
    )

    assert request.market_id == "m1"
    assert request.outcome is Outcome.NO
    assert request.side is VenueSide.SELL
    assert request.limit_price == "0.37"
    assert request.quantity == "12.5"


def test_adapter_does_not_silently_rewrite_yes_sell_into_no_buy() -> None:
    adapter = SigPredictionsMarketAdapter()
    request = adapter.to_venue_order(
        OrderIntent(
            instrument="SIG_PREDICTION:m1:YES",
            side=Side.ASK,
            price=Decimal("0.52"),
            size=Decimal("10"),
        ),
        SigMarketSpec(market_id="m1", outcome=Outcome.YES),
    )

    assert request.outcome is Outcome.YES
    assert request.side is VenueSide.SELL
    assert request.limit_price == "0.52"


@pytest.mark.parametrize("price", [Decimal("-0.01"), Decimal("1.01")])
def test_rejects_out_of_range_prediction_price(price: Decimal) -> None:
    adapter = SigPredictionsMarketAdapter()
    with pytest.raises(ValueError):
        adapter.to_venue_order(
            OrderIntent(
                instrument="SIG_PREDICTION:m1:YES",
                side=Side.BID,
                price=price,
                size=Decimal("1"),
            ),
            SigMarketSpec(market_id="m1", outcome=Outcome.YES),
        )


def test_rejects_reduce_only_until_venue_semantics_are_documented() -> None:
    adapter = SigPredictionsMarketAdapter()
    with pytest.raises(ValueError):
        adapter.to_venue_order(
            OrderIntent(
                instrument="SIG_PREDICTION:m1:YES",
                side=Side.BID,
                price=Decimal("0.4"),
                size=Decimal("1"),
                reduce_only=True,
            ),
            SigMarketSpec(market_id="m1", outcome=Outcome.YES),
        )


def test_execution_result_supports_opaque_order_id() -> None:
    adapter = SigPredictionsMarketAdapter()
    report = adapter.to_internal_execution_report(
        SigOrderResult(
            status="filled",
            order_id="ord_abc",
            filled_quantity="3",
            average_price="0.61",
        )
    )

    assert report.status is ExecutionStatus.FILLED
    assert report.exchange_order_id == "ord_abc"
    assert report.filled_size == Decimal("3")
    assert report.average_price == Decimal("0.61")


def test_realtime_channel_and_cancel_retry_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIG_PREDICTIONS_TOURNAMENT_ID", "t1")
    client = SigPredictionsVenueClient(SigPredictionsVenueConfig())

    assert client.realtime_market_channel("m1") == "tournament:t1:market:m1"
    assert client.should_retry_cancel(503)
    assert not client.should_retry_cancel(422)
