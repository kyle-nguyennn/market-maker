from decimal import Decimal

from exchanges.hyperliquid.types import HyperliquidMarketSpec
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter
from model.order import OrderIntent, Side


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
    assert isinstance(venue_order.price, str)
    assert isinstance(venue_order.size, str)
    assert venue_order.size == "0.1234"
