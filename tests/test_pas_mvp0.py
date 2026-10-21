from __future__ import annotations

from decimal import Decimal as D

import pytest

from execution.order_manager import OrderManager, ReconciliationError
from execution.pas_mvp0_runtime import (
    MVP0PasBot, PassiveQuoter, positions_from_perps, venue_status,
)
from execution.safe_gateway import SafeExecutionGateway
from execution.sqlite_journal import SQLiteWALJournal
from exchanges.hyperliquid.config import HyperliquidVenueConfig
from exchanges.hyperliquid.types import HyperliquidMarketSpec
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter
from model.market import BookLevel, MarketState
from model.order import Side
from risk.risk_manager import RiskManager, RiskLimits

WALLET = "0x" + "a" * 40


class FakeVenue:
    def __init__(self, position="0"):
        self.config = HyperliquidVenueConfig.from_environment(dex="pas", environ={})
        self.position = position
        self.open_orders = []
        self.fills = []
        self.status_calls = 0

    def query_positions(self, address):
        assert address == WALLET
        return {"assetPositions": [
            {"position": {"coin": "pas:BTC", "szi": self.position}}
        ], "marginSummary": {"accountValue": "1000"}}

    def query_open_orders(self, address):
        return list(self.open_orders)

    def query_active_asset_data(self, address, symbol):
        return {"maxTradeSzs": ["1", "1"]}

    def query_meta_and_asset_contexts(self):
        return ({"universe": [{"name": "pas:BTC"}]}, [{"markPx": "100"}])

    def query_fills(self, address):
        return list(self.fills)

    def query_order_status(self, address, *, client_order_id):
        self.status_calls += 1
        raise AssertionError("no orders expected in flat-start test")


class FakeMarket:
    def current(self):
        return MarketState(
            instrument="BTC", timestamp_ms=1_000,
            bids=(BookLevel(D("99"), D("1")),),
            asks=(BookLevel(D("101"), D("1")),),
        )


def make_bot(tmp_path, *, position="0"):
    venue = FakeVenue(position)
    adapter = HyperliquidMarketAdapter(venue.config)
    manager = OrderManager(dex="pas", account_id=WALLET)
    risk = RiskManager(RiskLimits(
        max_abs_position=D("1"), max_order_notional=D("100"),
        max_abs_notional=D("1000"), max_drawdown=D("50"),
        max_market_data_age_ms=2000,
    ))
    gateway = SafeExecutionGateway(
        manager=manager, risk=risk, adapter=adapter, client=venue,
        market_spec=HyperliquidMarketSpec(sz_decimals=4),
    )
    db = SQLiteWALJournal(tmp_path / "mvp0.sqlite", dex="pas", account_id=WALLET)
    bot = MVP0PasBot(
        client=venue, adapter=adapter, market=FakeMarket(), manager=manager,
        gateway=gateway, risk=risk, journal=db, team_wallet=WALLET,
        instrument="BTC", quoter=PassiveQuoter(size=D(".001"), edge_bps=D("5")),
        clock_ms=lambda: 20_000,
    )
    return bot, venue, db


def test_only_flat_account_can_create_new_ledger(tmp_path):
    bot, venue, journal = make_bot(tmp_path)
    try:
        bot._reconcile()
        assert bot.manager.ready and bot.ready_event.is_set()
        assert journal.load_anchor().result() == 20_000
        assert journal.load_baseline_equity().result() == D("1000")
        bot._reconcile()
        assert bot.manager.ready
        assert venue.status_calls == 0
    finally:
        journal.close()


def test_nonflat_cold_start_fails_closed(tmp_path):
    bot, _, journal = make_bot(tmp_path, position=".1")
    try:
        with pytest.raises(ReconciliationError, match="first boot requires flat"):
            bot._reconcile()
        assert not bot.ready_event.is_set()
        assert not bot.manager.ready
        assert journal.load_anchor().result() is None
    finally:
        journal.close()


def test_unmanaged_exchange_order_fails_closed(tmp_path):
    bot, venue, journal = make_bot(tmp_path)
    try:
        venue.open_orders.append({
            "coin": "pas:BTC", "cloid": "0x" + "b" * 32,
            "oid": 123, "limitPx": "100", "sz": "0.001", "side": "B",
        })
        with pytest.raises(ReconciliationError, match="unmanaged resting order"):
            bot._reconcile()
        assert not bot.manager.ready
    finally:
        journal.close()


def test_venue_order_status_requires_verifiable_nested_evidence():
    assert venue_status({
        "status": "order",
        "order": {"status": "filled", "order": {"oid": 12}}
    }) == ("filled", 12)
    with pytest.raises(ReconciliationError):
        venue_status({"status": "unknown", "order": {}})


def test_position_parser_rejects_missing_or_duplicate_positions():
    assert positions_from_perps({"assetPositions": []}) == {}
    with pytest.raises(ReconciliationError):
        positions_from_perps({})
    with pytest.raises(ReconciliationError):
        positions_from_perps({"assetPositions": [
            {"position": {"coin": "pas:BTC", "szi": "0"}},
            {"position": {"coin": "pas:BTC", "szi": "1"}},
        ]})


def test_quote_is_passive_and_never_submits_itself():
    model = PassiveQuoter(size=D("0.1"), edge_bps=D("10"))
    orders = model.desired(FakeMarket().current())
    assert [o.side for o in orders] == [Side.BID, Side.ASK]
    assert orders[0].price < D("99")
    assert orders[1].price > D("101")
