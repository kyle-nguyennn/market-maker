"""Stateful, bounded feature engine shared by replay and live bar producers.

The caller is responsible only for providing *normalized completed* PriceBar
events, whether from a historical feed or a live aggregator. Neither origin is
visible to FeatureEngine or its consumers. No network, database or SDK imports.
"""
from __future__ import annotations

from collections import deque
from typing import Sequence

from .calculators import FeatureCalculator
from .domain import FeatureSnapshot, _name, _timestamp
from .features import FeatureConfig, build_price_features
from .observations import PriceBar


class FeatureEngine:
    """Per-symbol rolling window and point-in-time quality gate.

    Only monotonic completed bars are accepted. Publication-late/out-of-order
    bars are rejected instead of retroactively changing an earlier state.
    A missing interval invalidates the rolling window, and the engine must
    accumulate a fresh contiguous lookback before emitting another feature.
    All storage is bounded to lookback_bars + 1 elements.
    """

    def __init__(
        self, *, symbol: str, config: FeatureConfig,
        calculator: FeatureCalculator | None = None,
    ) -> None:
        _name(symbol, "symbol")
        if not isinstance(config, FeatureConfig):
            raise TypeError("config must be FeatureConfig")
        self.symbol = symbol
        self.config = config
        self.calculator = calculator
        self._bars: deque[PriceBar] = deque(maxlen=config.lookback_bars + 1)
        self._last_end_ms: int | None = None
        self._source: str | None = None
        self._last_arrival_ms: int | None = None
        self.gaps_seen = 0

    @property
    def bars_buffered(self) -> int:
        return len(self._bars)

    def on_bar(self, bar: PriceBar, *, at_ms: int) -> FeatureSnapshot | None:
        """Consume one publication; return a snapshot only after warmup.

        at_ms is the time the engine receives/consumes the event, which may
        exceed vendor available_at_ms. It must not be earlier than that time.
        A live adapter and offline replay must both call this exact method.
        """
        if not isinstance(bar, PriceBar):
            raise TypeError("expected PriceBar")
        _timestamp(at_ms, "at_ms")
        if bar.symbol != self.symbol:
            raise ValueError("cross-symbol bar in feature engine")
        if bar.interval_ms != self.config.interval_ms:
            raise ValueError("unexpected bar interval")
        if bar.available_at_ms > at_ms:
            raise ValueError("bar not yet available at ingestion time")
        if self._source is not None and bar.source != self._source:
            raise ValueError("source/version changed; new engine required")
        if self._last_arrival_ms is not None and at_ms < self._last_arrival_ms:
            raise ValueError("ingestion clock moved backwards")
        if self._last_end_ms is not None and bar.end_ms <= self._last_end_ms:
            raise ValueError("out-of-order or duplicate completed bar")
        if self._last_end_ms is not None and (
            bar.end_ms - self._last_end_ms != self.config.interval_ms
        ):
            self._bars.clear()
            self.gaps_seen += 1

        self._source = bar.source
        self._last_end_ms = bar.end_ms
        self._last_arrival_ms = at_ms
        self._bars.append(bar)
        return self.snapshot(at_ms=at_ms)

    def snapshot(self, *, at_ms: int) -> FeatureSnapshot | None:
        """Recompute the current as-of snapshot without ingesting a new bar."""
        _timestamp(at_ms, "at_ms")
        if self._last_arrival_ms is not None and at_ms < self._last_arrival_ms:
            raise ValueError("snapshot requested before last ingested bar")
        if len(self._bars) < self.config.lookback_bars + 1:
            return None
        return build_price_features(
            tuple(self._bars), symbol=self.symbol, decision_at_ms=at_ms,
            config=self.config, calculator=self.calculator,
        )

    def reset(self) -> None:
        """Explicit restart/resynchronization; no unseen backfill."""
        self._bars.clear()
        self._last_end_ms = None
        self._source = None
        self._last_arrival_ms = None


def replay_bars(
    bars: Sequence[PriceBar], engine: FeatureEngine,
) -> tuple[FeatureSnapshot, ...]:
    """Offline adapter for the *same* FeatureEngine.on_bar API used live.

    Delivery is ordered by when the observation became available rather than
    its market timestamp. If delayed publication reorders completion times,
    the engine will fail closed, forcing an explicit revision/warmup policy.
    """
    snapshots = []
    for bar in sorted(bars, key=lambda b: (b.available_at_ms, b.end_ms)):
        result = engine.on_bar(bar, at_ms=bar.available_at_ms)
        if result is not None:
            snapshots.append(result)
    return tuple(snapshots)
