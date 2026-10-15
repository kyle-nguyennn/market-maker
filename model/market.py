from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class BookLevel:
    """Venue-independent book level.

    Internal numeric representation is always Decimal.
    """

    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class MarketState:
    """Venue-independent market snapshot consumed by strategies.

    Instrument names are logical/internal symbols such as BTC, not
    venue-qualified names such as par:BTC.
    """

    instrument: str
    timestamp_ms: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    last_trade_price: Decimal | None = None
    oracle_price: Decimal | None = None
    mark_price: Decimal | None = None
    funding_rate: Decimal | None = None
    open_interest: Decimal | None = None
    is_valid: bool = True
    invalid_reason: str | None = None

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal("2")
