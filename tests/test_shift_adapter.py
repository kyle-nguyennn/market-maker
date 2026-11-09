"""Offline contract tests. No SHIFT install, login, or exchange access required."""
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace

import pytest

from apps.hftc_shift import HftcLimits, HftcQuoter, _requests
from exchanges.shift.client import ShiftVenueClient
from market_handler.shift_adapter import ShiftMarketAdapter
from model.order import ExecutionStatus, OrderEventType, OrderIntent, Side


@dataclass
class Row:
    price: float
    size: int


def state(adapter, bids=None, asks=None):
    return adapter.snapshot(
        "AAPL", bids or [Row(100.01, 2), Row(100.00, 1)],
        asks or [Row(100.03, 3)], timestamp_ms=123, last_price=100.02,
    )


def test_shift_lots_and_order_book_are_converted_to_shares():
    adapter = ShiftMarketAdapter()
    s = adapter.snapshot(
        "AAPL", [Row(100.00, 2), Row(100.00, 3)],
        [Row(100.05, 4)], timestamp_ms=5,
    )
    assert s.is_valid
    assert s.bids[0].size == Decimal("500")
    assert s.asks[0].size == Decimal("400")
    assert s.mid == Decimal("100.025")
    assert s.timestamp_ms == 5


def test_tick_rounding_is_conservative_and_lots_exact():
    adapter = ShiftMarketAdapter()
    bid = adapter.to_venue_order(OrderIntent(
        "AAPL", Side.BID, Decimal("100.019"), Decimal("200"),
    ))
    ask = adapter.to_venue_order(OrderIntent(
        "AAPL", Side.ASK, Decimal("100.011"), Decimal("100"),
    ))
    assert (bid.price, bid.size_lots) == (100.01, 2)
    assert (ask.price, ask.size_lots) == (100.02, 1)
    with pytest.raises(ValueError, match="multiple of 100"):
        adapter.to_venue_order(OrderIntent(
            "AAPL", Side.BID, Decimal("100"), Decimal("250"),
        ))
    with pytest.raises(ValueError, match="reduce-only"):
        adapter.to_venue_order(OrderIntent(
            "AAPL", Side.ASK, Decimal("101"), Decimal("100"), reduce_only=True,
        ))


def test_non_marketable_check_uses_opposite_top_of_book():
    adapter = ShiftMarketAdapter()
    s = state(adapter)
    safe_bid = adapter.to_venue_order(OrderIntent(
        "AAPL", Side.BID, Decimal("100.02"), Decimal("100"),
    ))
    unsafe_bid = adapter.to_venue_order(OrderIntent(
        "AAPL", Side.BID, Decimal("100.03"), Decimal("100"),
    ))
    assert adapter.is_non_marketable(safe_bid, s)
    assert not adapter.is_non_marketable(unsafe_bid, s)


def test_crossed_book_blocks_quotes():
    adapter = ShiftMarketAdapter()
    s = state(adapter, bids=[Row(101, 1)], asks=[Row(100, 1)])
    assert not s.is_valid
    assert HftcQuoter(HftcLimits()).quote(s) == []


def test_shift_status_mapping_preserves_uuid_and_cumulative_shares():
    adapter = ShiftMarketAdapter()
    filled = SimpleNamespace(
        id="550e8400-e29b-41d4-a716-446655440000",
        symbol="AAPL", status=SimpleNamespace(name="PARTIALLY_FILLED"),
        executed_size=2, executed_price=100.55,
    )
    report = adapter.to_internal_execution_report(filled)
    assert report.exchange_order_id == filled.id
    assert report.status is ExecutionStatus.RESTING
    assert report.filled_size == Decimal(200)
    event = adapter.to_internal_order_event("local-id", filled)
    assert event.kind is OrderEventType.PARTIAL_FILL
    assert event.cumulative_filled_size == Decimal(200)


class FakeShift:
    class OrderBookType(Enum):
        GLOBAL_BID = 1
        GLOBAL_ASK = 2

    class Order:
        class Type(Enum):
            LIMIT_BUY = 1
            LIMIT_SELL = 2
            MARKET_BUY = 3
            MARKET_SELL = 4

        def __init__(self, kind, symbol, size, price=0):
            self.type, self.symbol, self.size, self.price = kind, symbol, size, price
            self.id = "synthetic-id"


class FakeTrader:
    def __init__(self):
        self.orders = []
        self.cancellations = []
        self.resting = []

    def get_stock_list(self):
        return ["AAPL"]

    def get_order_book(self, symbol, kind, depth):
        assert (symbol, depth) == ("AAPL", 10)
        return [Row(100.01, 2)] if kind == FakeShift.OrderBookType.GLOBAL_BID else [Row(100.03, 3)]

    def get_last_price(self, symbol):
        return 100.02

    def get_portfolio_items(self):
        return {"AAPL": SimpleNamespace(get_shares=lambda: 100)}

    def get_portfolio_summary(self):
        return SimpleNamespace(get_total_bp=lambda: 500000)

    def get_waiting_list(self):
        return self.resting

    def submit_order(self, order):
        self.orders.append(order)

    def submit_cancellation(self, order):
        self.cancellations.append(order)


def test_client_uses_shift_enum_and_does_not_convert_shares_twice():
    fake = FakeTrader()
    adapter = ShiftMarketAdapter()
    client = ShiftVenueClient(fake, FakeShift, adapter)
    s = client.snapshot("AAPL")
    assert s.bids[0].size == Decimal(200)
    assert client.position_shares("AAPL") == Decimal(100)
    assert client.buying_power() == Decimal(500000)
    req = adapter.to_venue_order(OrderIntent(
        "AAPL", Side.BID, Decimal("100.01"), Decimal("100"),
    ))
    assert client.submit_limit(req, latest=s) == "synthetic-id"
    assert len(fake.orders) == 1
    assert fake.orders[0].type == FakeShift.Order.Type.LIMIT_BUY
    assert fake.orders[0].size == 1
    assert fake.orders[0].price == 100.01
    fake.resting.append(fake.orders[0])
    assert client.cancel_symbol("AAPL") == 1
    assert fake.cancellations == fake.resting


def test_hftc_inventory_skew_and_position_limit():
    adapter = ShiftMarketAdapter()
    limits = HftcLimits(max_abs_position_shares=Decimal("100"))
    quoter = HftcQuoter(limits)
    s = state(adapter)
    neutral = quoter.quote(s)
    quoter.set_inventory(Decimal(100))
    long_quotes = quoter.quote(s)
    assert long_quotes[0].price < neutral[0].price
    requests = _requests(
        s, Decimal(100), Decimal("50000"), adapter, quoter, limits,
    )
    assert not any(r.side is Side.BID for r in requests)
