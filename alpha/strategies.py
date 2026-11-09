"""Elementary directional trading policy families, deliberately uncalibrated.

These are *decision* policies consuming validated AlphaSignals. The predictor
belongs to SignalEngine, not to these classes. No exchange or order imports.
"""
from __future__ import annotations

from decimal import Decimal

from .domain import TargetExposure, _confidence, _decimal
from .strategy import StrategyContext, StrategyDecision


class DirectionalSignalStrategy:
    """Threshold-and-fixed-weight policy for one signal family.

    A baseline only: weights are desired portfolio exposures, never orders.
    All thresholds/weights must subsequently be validated out of sample and
    constrained by downstream portfolio risk. Missing/weak signals cause
    ABSTENTION, not an implicit flatten of a current position.
    """

    def __init__(
        self, *, strategy_id: str, target_weight: Decimal,
        min_edge: Decimal = Decimal("0"),
        min_confidence: Decimal = Decimal("0"),
    ) -> None:
        if not strategy_id or not strategy_id.strip():
            raise ValueError("strategy_id required")
        _decimal(target_weight, "target_weight")
        _decimal(min_edge, "min_edge")
        _confidence(min_confidence)
        if target_weight <= 0 or min_edge < 0:
            raise ValueError("target_weight must be positive; min_edge nonnegative")
        self.strategy_id = strategy_id
        self.target_weight = target_weight
        self.min_edge = min_edge
        self.min_confidence = min_confidence

    def decide(self, context: StrategyContext) -> StrategyDecision:
        selected = [s for s in context.signals if s.strategy == self.strategy_id]
        if len(selected) > 1:
            raise ValueError("multiple forecasts for same strategy in one context")
        if not selected:
            return StrategyDecision(self.strategy_id, context.decision_at_ms)
        signal = selected[0]
        if (abs(signal.expected_return) <= self.min_edge
                or signal.confidence < self.min_confidence):
            return StrategyDecision(self.strategy_id, context.decision_at_ms)
        weight = self.target_weight if signal.expected_return > 0 else -self.target_weight
        desired = TargetExposure(
            decision_id=f"{self.strategy_id}:{signal.signal_id}",
            symbol=context.symbol, as_of_ms=context.decision_at_ms,
            horizon_ms=signal.horizon_ms, target_weight=weight,
            expected_return=signal.expected_return, confidence=signal.confidence,
            strategy=self.strategy_id, signal_ids=(signal.signal_id,),
            regime=signal.regime,
        )
        return StrategyDecision(
            strategy_id=self.strategy_id,
            decision_at_ms=context.decision_at_ms,
            exposures=(desired,),
        )


class MomentumStrategy(DirectionalSignalStrategy):
    """Baseline policy consuming the 'momentum' model's signed forecast."""

    def __init__(
        self, *, target_weight: Decimal, min_edge: Decimal = Decimal("0"),
        min_confidence: Decimal = Decimal("0"),
    ) -> None:
        super().__init__(
            strategy_id="momentum", target_weight=target_weight,
            min_edge=min_edge, min_confidence=min_confidence,
        )


class MeanReversionStrategy(DirectionalSignalStrategy):
    """Baseline policy consuming the 'mean_reversion' model's signed forecast."""

    def __init__(
        self, *, target_weight: Decimal, min_edge: Decimal = Decimal("0"),
        min_confidence: Decimal = Decimal("0"),
    ) -> None:
        super().__init__(
            strategy_id="mean_reversion", target_weight=target_weight,
            min_edge=min_edge, min_confidence=min_confidence,
        )
