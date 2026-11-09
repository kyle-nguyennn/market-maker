"""Source-agnostic normalized observation contracts for the alpha library.

All times are UTC epoch milliseconds. The end timestamp is the *completed*
bar end, never its start. The availability timestamp is when that bar could
first have been observed by the strategy; it must not predate bar end.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, Sequence

from .domain import _decimal, _horizon, _name, _timestamp


@dataclass(frozen=True, slots=True)
class PriceBar:
    symbol: str
    end_ms: int
    available_at_ms: int
    interval_ms: int
    source: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        _name(self.symbol, "symbol")
        _name(self.source, "source")
        _timestamp(self.end_ms, "end_ms")
        _timestamp(self.available_at_ms, "available_at_ms")
        _horizon(self.interval_ms, "interval_ms")
        if self.end_ms < self.interval_ms:
            raise ValueError("completed bar end must be at least one interval")
        if self.available_at_ms < self.end_ms:
            raise ValueError("bar cannot be published before it ends")
        for field in ("open", "high", "low", "close", "volume"):
            _decimal(getattr(self, field), field)
        if any(getattr(self, field) <= 0 for field in ("open", "high", "low", "close")):
            raise ValueError("OHLC prices must be positive")
        if self.volume < 0:
            raise ValueError("bar volume cannot be negative")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC range does not contain open and close")
        if self.low > self.high:
            raise ValueError("high must be >= low")


class BarProvider(Protocol):
    """Read-only provider returning completed bars, potentially including revisions.

    Feature construction always applies its own as-of availability filter.
    Providers must document survivorship, corporate-action adjustments,
    trading calendars, and whether historical values can be revised.
    """

    def get_bars(self, symbol: str, *, until_ms: int) -> Sequence[PriceBar]:
        ...

