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


def test_mocked_signed_order_fill_then_nonflat_restart(tmp_path):
    """Place/rest, fill while quoter is away, restart using durable ledger."""
    from execution.order_manager import OrderManager
    from execution.safe_gateway import SafeExecutionGateway
    from exchanges.hyperliquid.types import VenueOrderResult
    from model.order import OrderIntent

    class TradingVenue(FakeVenue):
        def __init__(self):
            super().__init__()
            self.order_statuses = {}
            self.deadman_times = []

        def schedule_cancel_all(self, deadline):
            self.deadman_times.append(deadline)

        def place_order(self, request):
            oid = 42
            self.open_orders.append({
                "coin": request.instrument, "cloid": request.client_order_id,
                "oid": oid, "side": "B", "limitPx": request.price, "sz": request.size,
            })
            self.order_statuses[request.client_order_id] = ("open", oid)
            return VenueOrderResult(status="resting", order_id=oid)

        def query_order_status(self, address, *, client_order_id):
            self.status_calls += 1
            status, oid = self.order_statuses[client_order_id]
            return {"status": "order", "order": {
                "status": status, "order": {"oid": oid}
            }}

        def cancel_order(self, instrument, *, client_order_id):
            self.open_orders = [
                x for x in self.open_orders if x["cloid"] != client_order_id
            ]
            self.order_statuses[client_order_id] = ("canceled", 42)
            return True

    class FreshMarket:
        def current(self):
            from model.market import MarketState, BookLevel
            return MarketState("BTC", 20_000,
                               (BookLevel(D("99"), D("1")),),
                               (BookLevel(D("101"), D("1")),))

    venue = TradingVenue()
    adapter = HyperliquidMarketAdapter(venue.config)
    limits = RiskLimits(
        max_abs_position=D("1"), max_order_notional=D("100"),
        max_abs_notional=D("1000"), max_drawdown=D("50"),
        max_market_data_age_ms=2000, min_order_notional=D(".01"),
        max_gross_notional=D("1000"), max_net_notional=D("1000"),
        max_quote_deviation_bps=D("100"),
    )

    def construct(journal):
        manager = OrderManager(dex="pas", account_id=WALLET)
        risk = RiskManager(limits)
        gateway = SafeExecutionGateway(
            manager=manager, risk=risk, adapter=adapter, client=venue,
            market_spec=HyperliquidMarketSpec(sz_decimals=4),
            live_enabled=True, operator_authorized=True,
        )
        return MVP0PasBot(
            client=venue, adapter=adapter, market=FreshMarket(),
            manager=manager, gateway=gateway, risk=risk, journal=journal,
            team_wallet=WALLET, instrument="BTC",
            quoter=PassiveQuoter(size=D(".01"), edge_bps=D("5")),
            live=True, verified_unified=True, clock_ms=lambda: 20_000,
        )

    path = tmp_path / "mvp0.sqlite"
    with SQLiteWALJournal(path, dex="pas", account_id=WALLET) as journal:
        bot = construct(journal)
        bot._reconcile()
        assert bot.manager.ready and venue.deadman_times == [110_000]
        intent = OrderIntent("BTC", Side.BID, D("99"), D(".01"))
        actions = bot.manager.reconcile([intent])
        assert len(actions) == 1
        bot._dispatch(actions[0])
        cloid = actions[0].intent.client_order_id
        assert cloid in venue.order_statuses, "risk-gated order must actually reach fake venue"
        bot._reconcile()
        assert bot.manager.ready and cloid in bot.manager.live_orders
        assert journal.load_all_placements().result()[0].exchange_order_id == 42
        assert journal.load_all_placements().result()[0].dispatch_state == "SENT"

        # Fill happens while the Quoter is not doing any work.
        venue.open_orders.clear()
        venue.order_statuses[cloid] = ("filled", 42)
        venue.position = ".01"
        venue.fills = [{
            "coin": "pas:BTC", "time": 21_000, "oid": 42, "tid": 1001,
            "sz": ".01", "px": "99", "fee": "0",
        }]
        bot._reconcile()
        assert bot.manager.ready
        assert journal.load_fill_positions().result()["BTC"] == D(".01")
        assert journal.load_all_placements().result()[0].dispatch_state == "CLOSED"

    # New OMS and SQLite writer must recover nonzero position, never reset anchor.
    with SQLiteWALJournal(path, dex="pas", account_id=WALLET) as journal:
        restarted = construct(journal)
        restarted._reconcile()
        assert restarted.manager.ready
        assert restarted.ready_event.is_set()
        assert journal.load_fill_positions().result()["BTC"] == D(".01")


def test_post_anchor_unmapped_fill_blocks_recovery(tmp_path):
    bot, venue, journal = make_bot(tmp_path)
    try:
        bot._reconcile()
        venue.fills.append({
            "coin": "pas:BTC", "time": 25_000, "oid": 999, "tid": 25,
            "sz": ".01", "px": "100", "fee": "0",
        })
        with pytest.raises(ReconciliationError, match="unmapped fill"):
            bot._reconcile()
        assert not bot.ready_event.is_set()
    finally:
        journal.close()
