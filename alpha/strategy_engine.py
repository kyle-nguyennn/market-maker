"""Evaluate different trading policy families against the same context.

StrategyEngine is a *research decision fan-out*, not a portfolio selector or
an execution engine. It deliberately keeps conflicting directional and
market-making proposals separate; a downstream policy must arbitrate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .domain import _name, _timestamp
from .strategy import Strategy, StrategyContext, StrategyDecision


@dataclass(frozen=True, slots=True)
class StrategyBatch:
    """Counterfactual independent decisions on one immutable market snapshot."""

    symbol: str
    snapshot_id: str
    decision_at_ms: int
    decisions: tuple[StrategyDecision, ...]
    skipped_reason: str | None = None

    def __post_init__(self) -> None:
        _name(self.symbol, "symbol")
        _name(self.snapshot_id, "snapshot_id")
        _timestamp(self.decision_at_ms, "decision_at_ms")
        if not isinstance(self.decisions, tuple):
            raise TypeError("decisions must be a tuple")
        if any(not isinstance(x, StrategyDecision) for x in self.decisions):
            raise TypeError("decisions must contain StrategyDecision")
        if any(x.decision_at_ms != self.decision_at_ms for x in self.decisions):
            raise ValueError("decision timestamp mismatch")
        if len({x.strategy_id for x in self.decisions}) != len(self.decisions):
            raise ValueError("duplicate strategy decision")
        if self.skipped_reason is not None:
            _name(self.skipped_reason, "skipped_reason")
            if any(x.exposures or x.quotes for x in self.decisions):
                raise ValueError("skipped batch must not contain active proposals")


class StrategyEngine:
    """Run registered Strategies with identical features/signals/portfolio.

    A batch includes one separate decision per strategy. No position
    aggregation, priority, simultaneous execution, order submission or trades.
    An explicitly invalid market abstains the whole batch; optional age gates
    let a caller declare maximum feature/market/portfolio input staleness.
    """

    def __init__(
        self, strategies: Sequence[Strategy], *,
        max_feature_age_ms: int | None = None,
        max_market_age_ms: int | None = None,
        max_portfolio_age_ms: int | None = None,
    ) -> None:
        self._strategies = tuple(strategies)
        if not self._strategies:
            raise ValueError("at least one Strategy required")
        names = []
        for strategy in self._strategies:
            if not isinstance(strategy, Strategy):
                raise TypeError("strategies must implement Strategy ABC")
            _name(strategy.strategy_id, "strategy_id")
            names.append(strategy.strategy_id)
        if len(set(names)) != len(names):
            raise ValueError("duplicate strategy_id")
        for key, age in (
            ("max_feature_age_ms", max_feature_age_ms),
            ("max_market_age_ms", max_market_age_ms),
            ("max_portfolio_age_ms", max_portfolio_age_ms),
        ):
            if age is not None and (type(age) is not int or age < 0):
                raise ValueError(f"{key} must be nonnegative int or None")
        self.max_feature_age_ms = max_feature_age_ms
        self.max_market_age_ms = max_market_age_ms
        self.max_portfolio_age_ms = max_portfolio_age_ms

    @property
    def strategy_ids(self) -> tuple[str, ...]:
        return tuple(x.strategy_id for x in self._strategies)

    def _skip(self, context: StrategyContext, reason: str) -> StrategyBatch:
        return StrategyBatch(
            symbol=context.symbol, snapshot_id=context.features.snapshot_id,
            decision_at_ms=context.decision_at_ms, skipped_reason=reason,
            decisions=tuple(
                StrategyDecision(x.strategy_id, context.decision_at_ms)
                for x in self._strategies
            ),
        )

    def evaluate(self, context: StrategyContext) -> StrategyBatch:
        if not isinstance(context, StrategyContext):
            raise TypeError("expected StrategyContext")
        if context.market is not None and not context.market.is_valid:
            return self._skip(context, "invalid_market")
        if (
            self.max_feature_age_ms is not None
            and context.decision_at_ms - context.features.available_at_ms
            > self.max_feature_age_ms
        ):
            return self._skip(context, "stale_features")
        if (
            context.market is not None and self.max_market_age_ms is not None
            and context.decision_at_ms - context.market.timestamp_ms
            > self.max_market_age_ms
        ):
            return self._skip(context, "stale_market")
        if (
            context.portfolio is not None and self.max_portfolio_age_ms is not None
            and context.decision_at_ms - context.portfolio.as_of_ms
            > self.max_portfolio_age_ms
        ):
            return self._skip(context, "stale_portfolio")

        known_signals = {s.signal_id for s in context.signals}
        decisions = []
        for strategy in self._strategies:
            decision = strategy.decide(context)
            if not isinstance(decision, StrategyDecision):
                raise TypeError("Strategy.decide must return StrategyDecision")
            if (decision.strategy_id != strategy.strategy_id
                    or decision.decision_at_ms != context.decision_at_ms):
                raise ValueError("strategy decision ID or time mismatch")
            for exposure in decision.exposures:
                if exposure.symbol != context.symbol:
                    raise ValueError("strategy exposure symbol mismatch")
                if exposure.strategy != strategy.strategy_id:
                    raise ValueError("strategy exposure owner mismatch")
                if not set(exposure.signal_ids).issubset(known_signals):
                    raise ValueError("strategy references unknown signal")
            for quote in decision.quotes:
                if quote.symbol != context.symbol:
                    raise ValueError("strategy quote symbol mismatch")
                if context.market is None:
                    raise ValueError("quoting strategy requires market state")
            decisions.append(decision)

        return StrategyBatch(
            symbol=context.symbol, snapshot_id=context.features.snapshot_id,
            decision_at_ms=context.decision_at_ms, decisions=tuple(decisions),
        )
