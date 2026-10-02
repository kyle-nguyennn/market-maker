from decimal import Decimal

from exchanges.hyperliquid.types import HyperliquidMarketSpec, VenueOrderResult
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter
from model.order import ExecutionStatus, OrderIntent, Side


def test_hyperliquid_snapshot_normalizes_to_internal_decimal_model() -> None:
    adapter = HyperliquidMarketAdapter()

    state = adapter.snapshot(
        instrument="par:BTC",
        timestamp_ms=1,
        bids=[("100.10", "0.25")],
        asks=[("100.20", "0.30")],
        oracle_price="100.15",
    )

    assert state.instrument == "BTC"
    assert state.best_bid == Decimal("100.10")
    assert state.best_ask == Decimal("100.20")
    assert state.oracle_price == Decimal("100.15")


def test_hyperliquid_outbound_conversion_is_adapter_owned() -> None:
    adapter = HyperliquidMarketAdapter()
    intent = OrderIntent(
        instrument="BTC",
        side=Side.BID,
        price=Decimal("12345.6789"),
        size=Decimal("0.123456"),
    )

    venue_order = adapter.to_venue_order(
        intent,
        HyperliquidMarketSpec(sz_decimals=4),
    )

    assert venue_order.instrument == "par:BTC"
    assert venue_order.price == "12345"
    assert venue_order.size == "0.1234"
    assert isinstance(venue_order.price, str)
    assert isinstance(venue_order.size, str)


def test_hyperliquid_ask_rounds_away_from_crossing() -> None:
    adapter = HyperliquidMarketAdapter()
    intent = OrderIntent(
        instrument="BTC",
        side=Side.ASK,
        price=Decimal("12345.0001"),
        size=Decimal("1"),
    )

    venue_order = adapter.to_venue_order(
        intent,
        HyperliquidMarketSpec(sz_decimals=4),
    )

    assert venue_order.price == "12346"


def test_hyperliquid_result_normalizes_back_to_decimal() -> None:
    adapter = HyperliquidMarketAdapter()

    report = adapter.to_internal_execution_report(
        VenueOrderResult(
            status="filled",
            order_id=42,
            filled_size="0.125",
            average_price="101.25",
        )
    )

    assert report.status is ExecutionStatus.FILLED
    assert report.exchange_order_id == 42
    assert report.filled_size == Decimal("0.125")
    assert report.average_price == Decimal("101.25")
