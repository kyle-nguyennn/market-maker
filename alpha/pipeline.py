"""Source-agnostic read-only alpha pipeline: feature -> signal -> strategy.

Every producer (historical replay, CSV playback, live bar aggregator) calls
the same on_bar(bar, at_ms=...) API. No strategy receives a feed type or a
history-store handle. No broker, order manager or network operations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .domain import AlphaSignal, FeatureSnapshot, _timestamp
from .feature_engine import FeatureEngine
from .observations import PriceBar
from .signal_engine import SignalEngine
from .strategy import MarketView, PortfolioSnapshot, Strategy, StrategyContext, StrategyDecision


@dataclass(frozen=True, slots=True)
class PipelineResult:
    features: FeatureSnapshot
    signals: tuple[AlphaSignal, ...]
    decision: StrategyDecision


class AlphaPipeline:
    """One symbol, one chosen Strategy; model and execution are separate.

    Return None during feature warmup. A strategy may also explicitly abstain
    by returning an empty StrategyDecision. The result is NOT a submitted
    market order; downstream portfolio/risk/execution gates are mandatory.
    """

    def __init__(
        self, *, features: FeatureEngine, signals: SignalEngine, strategy: Strategy,
    ) -> None:
        self.features = features
        self.signals = signals
        self.strategy = strategy

    def on_bar(
        self, bar: PriceBar, *, at_ms: int,
        market: MarketView | None = None,
        portfolio: PortfolioSnapshot | None = None,
    ) -> PipelineResult | None:
        snapshot = self.features.on_bar(bar, at_ms=at_ms)
        if snapshot is None:
            return None
        predictions = self.signals.on_features(snapshot, generated_at_ms=at_ms)
        context = StrategyContext(
            symbol=snapshot.symbol, decision_at_ms=at_ms,
            features=snapshot, signals=predictions,
            market=market, portfolio=portfolio,
        )
        decision = self.strategy.decide(context)
        if not isinstance(decision, StrategyDecision):
            raise TypeError("Strategy.decide must return StrategyDecision")
        if (decision.strategy_id != self.strategy.strategy_id
                or decision.decision_at_ms != at_ms):
            raise ValueError("strategy decision id or timestamp mismatch")
        valid_ids = {signal.signal_id for signal in predictions}
        for target in decision.exposures:
            if (target.symbol != snapshot.symbol
                    or not set(target.signal_ids).issubset(valid_ids)):
                raise ValueError("target references unknown signals or symbol")
        for quote in decision.quotes:
            if quote.symbol != snapshot.symbol:
                raise ValueError("quote references another symbol")
        return PipelineResult(snapshot, predictions, decision)


def replay_pipeline(
    bars: Sequence[PriceBar], pipeline: AlphaPipeline,
) -> tuple[PipelineResult, ...]:
    """Replay adapter using precisely the same on_bar interface as live feeds.

    External historical joins/market-context replay are future work; this
    only replays bar events. Replay cannot simulate fills or portfolio changes.
    """
    results = []
    for bar in sorted(bars, key=lambda b: (b.available_at_ms, b.end_ms)):
        emitted = pipeline.on_bar(bar, at_ms=bar.available_at_ms)
        if emitted is not None:
            results.append(emitted)
    return tuple(results)
