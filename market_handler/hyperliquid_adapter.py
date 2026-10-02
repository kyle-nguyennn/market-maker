from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from exchanges.hyperliquid.config import HyperliquidVenueConfig


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class HyperliquidMarketState:
    """Normalized Perpetual Alpha market snapshot.

    Do not expose raw Hyperliquid SDK structures beyond the adapter.
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
        return (self.best_bid + self.best_ask) / Decimal(2)


class HyperliquidMarketAdapter:
    """Translate Hyperliquid venue messages into normalized domain state."""

    def __init__(self, config: HyperliquidVenueConfig | None = None) -> None:
        self.config = config or HyperliquidVenueConfig()

    def instrument(self, symbol: str) -> str:
        return self.config.venue_symbol(symbol)

    @staticmethod
    def levels(rows: Iterable[tuple[str, str]]) -> tuple[BookLevel, ...]:
        return tuple(BookLevel(Decimal(px), Decimal(sz)) for px, sz in rows)

    def snapshot(
        self,
        *,
        instrument: str,
        timestamp_ms: int,
        bids: Iterable[tuple[str, str]],
        asks: Iterable[tuple[str, str]],
        last_trade_price: str | None = None,
        oracle_price: str | None = None,
        mark_price: str | None = None,
        funding_rate: str | None = None,
        open_interest: str | None = None,
    ) -> HyperliquidMarketState:
        bid_levels = self.levels(bids)
        ask_levels = self.levels(asks)

        invalid_reason = None
        if not bid_levels or not ask_levels:
            invalid_reason = "missing_book_side"
        elif bid_levels[0].price >= ask_levels[0].price:
            invalid_reason = "crossed_book"

        return HyperliquidMarketState(
            instrument=self.instrument(instrument),
            timestamp_ms=timestamp_ms,
            bids=bid_levels,
            asks=ask_levels,
            last_trade_price=Decimal(last_trade_price) if last_trade_price else None,
            oracle_price=Decimal(oracle_price) if oracle_price else None,
            mark_price=Decimal(mark_price) if mark_price else None,
            funding_rate=Decimal(funding_rate) if funding_rate else None,
            open_interest=Decimal(open_interest) if open_interest else None,
            is_valid=invalid_reason is None,
            invalid_reason=invalid_reason,
        )
