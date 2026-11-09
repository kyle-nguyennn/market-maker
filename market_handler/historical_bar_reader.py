"""Historical data-source adapter supplying normalized alpha observations.

Reader code belongs in the host's market-data layer, not in alpha/. This
offline CSV reader is one input option, not an AlphArena market-data feed;
live and historical adapters must publish the same PriceBar contract.
"""
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from alpha.observations import PriceBar

_COLUMNS = (
    "symbol", "end_ms", "available_at_ms",
    "open", "high", "low", "close", "volume",
)


def read_price_bars_csv(
    path: str | Path, *, interval_ms: int, source: str,
) -> tuple[PriceBar, ...]:
    """Load vendor-normalized CSV OHLCV bars preserving exact Decimal strings.

    Header must have exactly the eight expected columns (order arbitrary).
    Explicit dataset/source revision, bar interval and publication timestamp
    are required; there is no guessed timezone, adjustment or gap filling.
    This adapter performs ingestion; FeatureEngine owns its temporal window.
    """
    if type(interval_ms) is not int or interval_ms <= 0:
        raise ValueError("interval_ms must be a positive integer")
    if not isinstance(source, str) or not source.strip() or source != source.strip():
        raise ValueError("source must be a non-empty trimmed string")
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
