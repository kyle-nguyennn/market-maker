"""Thin synchronous SHIFT client; SDK is loaded only on actual VM connection."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from market_handler.shift_adapter import ShiftMarketAdapter, ShiftOrderRequest
from model.market import MarketState
from model.order import Side


class ShiftVenueClient:
    def __init__(self, trader: Any, sdk: Any, adapter: ShiftMarketAdapter):
        self.trader = trader
        self.sdk = sdk
        self.adapter = adapter

    @classmethod
    def connect(
        cls, *, username: str, password: str, initiator_cfg: str,
        adapter: ShiftMarketAdapter,
        sdk: Any = None,
    ) -> "ShiftVenueClient":
        if sdk is None:
            import shift as sdk  # available inside the competition-provided environment
        trader = sdk.Trader(username)
        try:
            if trader.connect(initiator_cfg, password) is False:
                raise RuntimeError("SHIFT authentication/connection failed")
            trader.sub_all_order_book()
        except Exception:
            trader.disconnect()
            raise
        return cls(trader, sdk, adapter)

    def close(self) -> None:
        self.trader.disconnect()

    def symbols(self) -> list[str]:
        return list(self.trader.get_stock_list())

    def snapshot(self, symbol: str, depth: int = 10) -> MarketState:
        bid_type = self.sdk.OrderBookType.GLOBAL_BID
        ask_type = self.sdk.OrderBookType.GLOBAL_ASK
        bids = self.trader.get_order_book(symbol, bid_type, depth)
        asks = self.trader.get_order_book(symbol, ask_type, depth)
        last_price = self.trader.get_last_price(symbol)
        return self.adapter.snapshot(symbol, bids, asks, last_price=last_price)

    def position_shares(self, symbol: str) -> Decimal:
        items = self.trader.get_portfolio_items()
        if symbol not in items:
            return Decimal(0)
        # SHIFT portfolio amounts are in shares, unlike order sizes.
        return Decimal(str(items[symbol].get_shares()))

    def buying_power(self) -> Decimal:
        return Decimal(str(self.trader.get_portfolio_summary().get_total_bp()))

    def pending_orders(self, symbol: str) -> list[Any]:
        return [order for order in self.trader.get_waiting_list() if order.symbol == symbol]

    def submitted_orders(self, symbol: str) -> list[Any]:
        return [order for order in self.trader.get_submitted_orders() if order.symbol == symbol]

    def submit_limit(self, request: ShiftOrderRequest, *, latest: MarketState) -> str:
        if not self.adapter.is_non_marketable(request, latest):
            raise ValueError("post-only emulation refused crossed/stale quote")
        kind = (self.sdk.Order.Type.LIMIT_BUY if request.side is Side.BID
                else self.sdk.Order.Type.LIMIT_SELL)
        order = self.sdk.Order(kind, request.symbol, request.size_lots, request.price)
        # submit_order has no acknowledgment; the returned id is for later reconciliation.
        self.trader.submit_order(order)
        if not order.id:
            raise RuntimeError("SHIFT submitted order has no id; reconcile before retry")
        return str(order.id)

    def submit_market_close(self, symbol: str, position_shares: Decimal) -> str | None:
        if position_shares == 0:
            return None
        lots = abs(position_shares) / Decimal(100)
        if lots != lots.to_integral_value() or lots <= 0:
            raise ValueError("cannot market-close position not aligned to 100 shares")
        kind = (self.sdk.Order.Type.MARKET_SELL if position_shares > 0
                else self.sdk.Order.Type.MARKET_BUY)
        order = self.sdk.Order(kind, symbol, int(lots))
        self.trader.submit_order(order)
        if not order.id:
            raise RuntimeError("SHIFT market-close id missing; reconcile before retry")
        return str(order.id)

    def cancel_symbol(self, symbol: str) -> int:
        """Requests cancellation. Caller MUST wait for a cleared waiting list."""
        pending = self.pending_orders(symbol)
        for order in pending:
            self.trader.submit_cancellation(order)
        return len(pending)

    def execution_count(self, symbol: str) -> int:
        """Count individual executions, not submitted or resting orders."""
        total = 0
        for order in self.submitted_orders(symbol):
            total += len(self.trader.get_executed_orders(str(order.id)))
        return total
