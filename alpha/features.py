"""Point-in-time, deterministic price-bar feature construction.

Deliberately supports uniformly spaced bars only. Missing bars, market
sessions/holidays, revisions, adjusted prices and cross-asset alignment must
be handled explicitly by an upstream, tested data-normalization layer before
this function is applied. It does *not* backfill absent prices.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Sequence

from .domain import FeatureSnapshot, _horizon, _name, _timestamp
from .market_data import PriceBar


@dataclass(frozen=True, slots=True)
class FeatureConfig:
    interval_ms: int
    lookback_bars: int
    max_staleness_ms: int
    feature_version: str

    def __post_init__(self) -> None:
        _horizon(self.interval_ms, "interval_ms")
        if type(self.lookback_bars) is not int or self.lookback_bars < 2:
            raise ValueError("lookback_bars must be an integer >= 2")
        if type(self.max_staleness_ms) is not int or self.max_staleness_ms < 0:
            raise ValueError("max_staleness_ms must be a non-negative integer")
        _name(self.feature_version, "feature_version")


def _snapshot_id(
    *, symbol: str, decision_at_ms: int, config: FeatureConfig,
    history: tuple[PriceBar, ...],
) -> str:
    """SHA-256 over canonical config and the *actual* input observations.

    No local file path, random seed, wall-clock time, Python hash(), or float
    representation enters the ID. Changing the observed values, source,
    publication time or feature config changes provenance.
    """
    identity = {
        "symbol": symbol,
        "decision_at_ms": decision_at_ms,
        "config": {
            "interval_ms": config.interval_ms,
            "lookback_bars": config.lookback_bars,
            "max_staleness_ms": config.max_staleness_ms,
            "feature_version": config.feature_version,
        },
        "bars": [
            {
                "symbol": bar.symbol, "source": bar.source,
                "end_ms": bar.end_ms, "available_at_ms": bar.available_at_ms,
                "interval_ms": bar.interval_ms,
                "open": str(bar.open), "high": str(bar.high),
                "low": str(bar.low), "close": str(bar.close),
                "volume": str(bar.volume),
            }
            for bar in history
        ],
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def build_price_features(
    bars: Sequence[PriceBar],
    *, symbol: str, decision_at_ms: int, config: FeatureConfig,
) -> FeatureSnapshot:
    """Build a price-feature vector as actually observable at decision_at_ms.

    Only bars with available_at_ms <= decision_at_ms can influence the
    snapshot. Input may be unsorted and may contain future data; neither
    should affect the point-in-time result. The final lookback_bars + 1
    eligible bars must be consecutive, completed, same-source observations.

    Features (all dimensionless, all finite Decimal):
      return_1: last close-to-close simple return
      momentum: close change over lookback_bars periods
      realized_vol: population std dev of those one-period simple returns
      price_zscore: current close vs the last lookback_bars closes,
                    using population standard deviation (zero for flat bars)

    Volatility is per bar, not annualized. These are descriptive observations,
    *not predictions*, and must be interpreted at the declared sampling rate.
    """
    _name(symbol, "symbol")
    _timestamp(decision_at_ms, "decision_at_ms")
    if not isinstance(config, FeatureConfig):
        raise TypeError("config must be FeatureConfig")

    eligible = sorted(
        (bar for bar in bars if bar.symbol == symbol
         and bar.available_at_ms <= decision_at_ms
         and bar.end_ms <= decision_at_ms),
        key=lambda bar: bar.end_ms,
    )
    if len(eligible) < config.lookback_bars + 1:
        raise ValueError("insufficient eligible history at decision time")
    # Reject duplicate end timestamps even outside the calculation window:
    # the snapshot must not silently choose between competing revisions.
    if any(left.end_ms == right.end_ms for left, right in zip(eligible, eligible[1:])):
        raise ValueError("duplicate available bars: revision policy is undefined")

    history = tuple(eligible[-(config.lookback_bars + 1):])
    source = history[0].source
    if any(bar.source != source or bar.interval_ms != config.interval_ms
           for bar in history):
        raise ValueError("mixed sources or bar intervals in feature lookback")
    if any(right.end_ms - left.end_ms != config.interval_ms
           for left, right in zip(history, history[1:])):
        raise ValueError("non-contiguous bars: no silent gap filling")
    if decision_at_ms - history[-1].end_ms > config.max_staleness_ms:
        raise ValueError("latest eligible bar is stale")

    # Fixed precision makes results reproducible even if the process-local
    # Decimal context was changed by unrelated code.
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        prices = [bar.close for bar in history]
        unit = Decimal("1")
        returns = [
            prices[i] / prices[i - 1] - unit
            for i in range(1, len(prices))
        ]
        n = Decimal(len(returns))
        r_mean = sum(returns, Decimal("0")) / n
        r_variance = sum(((r - r_mean) ** 2 for r in returns), Decimal("0")) / n
        closes = prices[-config.lookback_bars:]
        p_count = Decimal(len(closes))
        p_mean = sum(closes, Decimal("0")) / p_count
        p_variance = sum(((p - p_mean) ** 2 for p in closes), Decimal("0")) / p_count
        values = {
            "return_1": returns[-1],
            "momentum": prices[-1] / prices[0] - unit,
            "realized_vol": r_variance.sqrt(),
            "price_zscore": (
                (prices[-1] - p_mean) / p_variance.sqrt()
                if p_variance != 0 else Decimal("0")
            ),
        }

    return FeatureSnapshot(
        snapshot_id=_snapshot_id(
            symbol=symbol, decision_at_ms=decision_at_ms,
            config=config, history=history,
        ),
        symbol=symbol,
        observed_at_ms=history[-1].end_ms,
        available_at_ms=max(bar.available_at_ms for bar in history),
        lookback_ms=config.interval_ms * config.lookback_bars,
        source=source,
        feature_version=config.feature_version,
        values=values,
    )
