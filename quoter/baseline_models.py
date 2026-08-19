from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from market_handler.market_adapter import MarketState
from quoter.base_quoter import BaseMMQuoter, Quote, Side


class FixedQuoteStrategy(BaseMMQuoter):
    """Reference implementation: fixed fair value and fixed spread."""

    def __init__(self, reference_price: int = 50, spread_ticks: int = 4, max_qty: int = 10) -> None:
        self.reference_price = int(reference_price)
        self.spread_ticks = max(1, int(spread_ticks))
        self.max_qty = max(1, int(max_qty))

    def fair_price(self, state: MarketState) -> int:
        return self.reference_price

    def reservation_price(self, state: MarketState, fair: int) -> int:
        return fair

    def calculate_spread(self, state: MarketState, fair: int, reservation: int) -> tuple[int, int]:
        half = max(1, self.spread_ticks // 2)
        return half, half

    def calculate_size(self, state: MarketState, bid_price: int, ask_price: int) -> tuple[int, int]:
        return self.max_qty, self.max_qty

    def quote(self, state: MarketState):
        if not state.is_valid:
            return [Quote(0, 0, Side.BID), Quote(0, 0, Side.ASK)]

        fair = self.fair_price(state)
        bid = fair - self.spread_ticks // 2
        ask = fair + self.spread_ticks - self.spread_ticks // 2
        qty = self.max_qty
        return [Quote(bid, qty, Side.BID), Quote(ask, qty, Side.ASK)]


class MidpointMMQuoter(BaseMMQuoter):
    """Simple baseline where fair value is the book midpoint."""

    def __init__(self, spread_ticks: int = 4, max_qty: int = 10) -> None:
        self.spread_ticks = max(1, int(spread_ticks))
        self.max_qty = max(1, int(max_qty))

    def fair_price(self, state: MarketState) -> int:
        if not state.ask_prices or not state.bid_prices:
            return 50
        return state.mid_price

    def reservation_price(self, state: MarketState, fair: int) -> int:
        return fair

    def calculate_spread(self, state: MarketState, fair: int, reservation: int) -> tuple[int, int]:
        half = max(1, self.spread_ticks // 2)
        return half, half

    def calculate_size(self, state: MarketState, bid_price: int, ask_price: int) -> tuple[int, int]:
        return self.max_qty, self.max_qty


class InventoryAwareMMQuoter(MidpointMMQuoter):
    """Midpoint strategy with inventory skew controlled by signed position."""

    def __init__(self, spread_ticks: int = 4, inventory_penalty: float = 0.05, max_qty: int = 10) -> None:
        super().__init__(spread_ticks=spread_ticks, max_qty=max_qty)
        self.inventory_penalty = float(inventory_penalty)

    def reservation_price(self, state: MarketState, fair: int) -> int:
        skew = state.inventory * self.inventory_penalty * 100
        return int(round(fair - skew))

    def quote(self, state: MarketState):
        if not state.is_valid:
            return [Quote(0, 0, Side.BID), Quote(0, 0, Side.ASK)]

        fair = self.fair_price(state)
        reservation = self.reservation_price(state, fair)
        bid_offset, ask_offset = self.calculate_spread(state, fair, reservation)
        bid_price = reservation - bid_offset
        ask_price = reservation + ask_offset
        bid_size, ask_size = self.calculate_size(state, bid_price, ask_price)
        return [Quote(bid_price, bid_size, Side.BID), Quote(ask_price, ask_size, Side.ASK)]


class VolatilityAwareMMQuoter(MidpointMMQuoter):
    """Baseline spread widening with realized or configured volatility."""

    def __init__(self, spread_ticks: int = 4, volatility: float = 0.2, max_qty: int = 10) -> None:
        super().__init__(spread_ticks=spread_ticks, max_qty=max_qty)
        self.volatility = max(0.0, float(volatility))

    def calculate_spread(self, state: MarketState, fair: int, reservation: int) -> tuple[int, int]:
        width = max(self.spread_ticks, int(round(self.volatility * 100)))
        half = max(1, width // 2)
        return half, half
