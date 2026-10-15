from decimal import Decimal

import pytest

from exchanges.hyperliquid.types import HyperliquidMarketSpec, VenueOrderResult
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter
from model.order import ExecutionStatus, OrderIntent, Side


@pytest.fixture(autouse=True)
def _default_practice_profile(monkeypatch):
    # Keep legacy par model tests independent of the user's selected .env.
    monkeypatch.setenv("PERPETUAL_ALPHA_DEX", "par")


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


def test_real_sdk_l2_and_context_normalize_to_decimal_state() -> None:
    adapter = HyperliquidMarketAdapter()
    book = {
        "coin": "par:BTC",
        "time": 123456789,
        "levels": [
            [{"px": "65000.1", "sz": "0.1200", "n": 3}],
            [{"px": "65000.2", "sz": "0.2300", "n": 2}],
        ],
    }
    context = {
        "oraclePx": "65000.15",
        "markPx": "65000.16",
        "funding": "0.000001",
        "openInterest": "1250.01",
    }
    state = adapter.from_sdk_l2_snapshot(book, asset_ctx=context)
    assert state.instrument == "BTC"
    assert state.timestamp_ms == 123456789
    assert state.best_bid == Decimal("65000.1")
    assert state.best_ask == Decimal("65000.2")
    assert state.oracle_price == Decimal("65000.15")
    assert state.mark_price == Decimal("65000.16")
    assert state.funding_rate == Decimal("0.000001")
    assert state.open_interest == Decimal("1250.01")
    assert state.is_valid


def test_sdk_metadata_resolves_lot_precision() -> None:
    adapter = HyperliquidMarketAdapter()
    info = {"universe": [
        {"name": "par:BTC", "szDecimals": 4},
        {"name": "par:ETH", "szDecimals": 4},
    ]}
    spec = adapter.market_spec_from_meta("BTC", info)
    assert spec.sz_decimals == 4
    with __import__("pytest").raises(ValueError, match="not found"):
        adapter.market_spec_from_meta("HYPE", info)


def test_sdk_snapshot_crossed_book_is_rejected() -> None:
    adapter = HyperliquidMarketAdapter()
    state = adapter.from_sdk_l2_snapshot({
        "coin": "par:BTC", "time": 2,
        "levels": [[{"px": "100", "sz": "1"}], [{"px": "99", "sz": "1"}]],
    })
    assert not state.is_valid
    assert state.invalid_reason == "crossed_book"
