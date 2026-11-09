"""Immutable OHLCV bars and strict, reproducible CSV ingestion.

All times are UTC epoch milliseconds. The end timestamp is the *completed*
bar end, never its start. The availability timestamp is when that bar could
first have been observed by the strategy; it must not predate bar end.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
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


_COLUMNS = (
    "symbol", "end_ms", "available_at_ms",
    "open", "high", "low", "close", "volume",
)


def read_price_bars_csv(
    path: str | Path, *, interval_ms: int, source: str,
) -> tuple[PriceBar, ...]:
    """Load vendor-normalized bars; preserve exact numeric strings as Decimal.

    CSV columns must exactly match _COLUMNS (order arbitrary). No implicit
    timezone parsing, price adjustment, gap filling, availability estimation,
    or deduplication. Caller supplies source identifying dataset *version*.
    The downstream builder detects duplicated time buckets and market gaps.
    """
    _horizon(interval_ms, "interval_ms")
    _name(source, "source")
    bars: list[PriceBar] = []
    with Path(path).open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or (
            len(reader.fieldnames) != len(_COLUMNS)
            or set(reader.fieldnames) != set(_COLUMNS)
        ):
            raise ValueError("unexpected CSV header; exact OHLCV schema required")
        for row_number, row in enumerate(reader, start=2):
            try:
                if None in row or any(
                    not isinstance(value, str) or not value.strip()
                    for value in row.values()
                ):
                    raise ValueError("missing or extra CSV field")
                bars.append(PriceBar(
                    symbol=row["symbol"],
                    end_ms=int(row["end_ms"]),
                    available_at_ms=int(row["available_at_ms"]),
                    interval_ms=interval_ms,
                    source=source,
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=Decimal(row["volume"]),
                ))
            except (ValueError, TypeError, ArithmeticError) as exc:
                raise ValueError(f"invalid OHLCV CSV row {row_number}") from exc
    return tuple(bars)
