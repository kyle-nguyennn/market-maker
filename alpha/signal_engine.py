"""Independent signal inference engine; feature origin is irrelevant.

Signal models are pure prediction policies. Trading strategies are separate
consumers of these signals and can generate target exposures or quote proposals.
"""
from __future__ import annotations

from typing import Sequence

from .domain import AlphaSignal, FeatureSnapshot, _name, _timestamp
from .strategy import SignalModel


class SignalEngine:
    """Evaluate registered models against one immutable feature snapshot.

    Each model may abstain. All outputs are checked against snapshot provenance
    and the injected decision clock before being published. The engine holds
    only the most recent per-symbol signal set (not a feature/history store).
    """

    def __init__(self, models: Sequence[SignalModel]) -> None:
        self._models = tuple(models)
        names = []
        for model in self._models:
            name = model.strategy_id
            _name(name, "signal model strategy_id")
            names.append(name)
        if len(set(names)) != len(names):
            raise ValueError("duplicate signal model strategy_id")
        self._latest: dict[str, tuple[AlphaSignal, ...]] = {}
        self._last_observed: dict[str, int] = {}

    def on_features(
        self, features: FeatureSnapshot, *, generated_at_ms: int,
    ) -> tuple[AlphaSignal, ...]:
        if not isinstance(features, FeatureSnapshot):
            raise TypeError("expected FeatureSnapshot")
        _timestamp(generated_at_ms, "generated_at_ms")
        if features.available_at_ms > generated_at_ms:
            raise ValueError("features not yet available at signal generation time")
        previous = self._last_observed.get(features.symbol)
        if previous is not None and features.observed_at_ms < previous:
            raise ValueError("signal source time moved backwards")
        output = []
        seen = set()
        for model in self._models:
            result = model.predict(features, generated_at_ms=generated_at_ms)
            if result is None:
                continue
            if not isinstance(result, AlphaSignal):
                raise TypeError("signal model must return AlphaSignal or None")
            if (
                result.symbol != features.symbol
                or result.snapshot_id != features.snapshot_id
                or result.feature_available_at_ms != features.available_at_ms
                or result.generated_at_ms != generated_at_ms
                or result.strategy != model.strategy_id
            ):
                raise ValueError("signal provenance or clock mismatch")
            if result.signal_id in seen:
                raise ValueError("duplicate signal_id")
            seen.add(result.signal_id)
            output.append(result)
        signals = tuple(output)
        self._latest[features.symbol] = signals
        self._last_observed[features.symbol] = features.observed_at_ms
        return signals

    def latest(self, symbol: str) -> tuple[AlphaSignal, ...]:
        _name(symbol, "symbol")
        return self._latest.get(symbol, ())
