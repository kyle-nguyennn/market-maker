"""
Normalize market data from different exchanges.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MarketState:
    """
    Lightweight normalized market state that both the simulator and venue adapters
    can convert into before passing to a quoter.
    """

    instrument: str | None = None
    timestamp: float = 0.0
    sequence: int = 0
    bid_prices: list[int] = field(default_factory=list)
    bid_volumes: list[int] = field(default_factory=list)
    ask_prices: list[int] = field(default_factory=list)
    ask_volumes: list[int] = field(default_factory=list)
    last_trade_price: int | None = None
    last_trade_side: str | None = None
    time_remaining: float | None = None
    is_valid: bool = True
    inventory: int = 0

    @property
    def best_bid(self) -> int:
        return self.bid_prices[0] if self.bid_prices else 0

    @property
    def best_ask(self) -> int:
        return self.ask_prices[0] if self.ask_prices else 0

    @property
    def mid_price(self) -> int:
        if not self.bid_prices or not self.ask_prices:
            return 0
        return int((self.best_bid + self.best_ask) / 2)

    @property
    def spread(self) -> int:
        if not self.bid_prices or not self.ask_prices:
            return 0
        return max(0, self.best_ask - self.best_bid)