"""Versioned, deterministic *hypothesis* models for directional alpha research.

The predictors turn already-computed features into signed return forecasts.
Their coefficients are explicit assumptions, NOT empirically calibrated.
Neither model imports market-data handlers, orders, SDKs or execution code.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext

from .domain import (
    AlphaSignal, FeatureSnapshot, _confidence, _decimal, _horizon, _name,
    _timestamp,
)


def _validate_model(
    *, feature_version: str, model_version: str, horizon_ms: int,
    forecast_multiplier: Decimal, min_abs_feature: Decimal, model_score: Decimal,
) -> None:
    _name(feature_version, "feature_version")
    _name(model_version, "model_version")
    _horizon(horizon_ms, "horizon_ms")
    _decimal(forecast_multiplier, "forecast_multiplier")
    _decimal(min_abs_feature, "min_abs_feature")
    _confidence(model_score)
    if forecast_multiplier <= 0:
        raise ValueError("forecast_multiplier must be strictly positive")
    if min_abs_feature < 0:
        raise ValueError("minimum absolute feature must be nonnegative")


def _make_signal(
    snapshot: FeatureSnapshot, *, generated_at_ms: int,
    strategy_id: str, model_version: str, horizon_ms: int,
    expected_return: Decimal, model_score: Decimal,
    configuration: dict[str, str],
) -> AlphaSignal:
    _timestamp(generated_at_ms, "generated_at_ms")
    if generated_at_ms < snapshot.available_at_ms:
        raise ValueError("signal cannot use features unavailable at generation")
    payload = {
        "strategy": strategy_id,
        "model_version": model_version,
        "feature_snapshot_id": snapshot.snapshot_id,
        "generated_at_ms": generated_at_ms,
        "horizon_ms": horizon_ms,
        "parameters": configuration,
        "expected_return": str(expected_return),
        "model_score": str(model_score),
    }
    identity = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    signal_id = strategy_id + ":" + hashlib.sha256(identity.encode("ascii")).hexdigest()
    return AlphaSignal(
        signal_id=signal_id, snapshot_id=snapshot.snapshot_id,
        symbol=snapshot.symbol, strategy=strategy_id,
        model_version=model_version, generated_at_ms=generated_at_ms,
        feature_available_at_ms=snapshot.available_at_ms,
        horizon_ms=horizon_ms, expected_return=expected_return,
        confidence=model_score,
    )


@dataclass(frozen=True, slots=True)
class MomentumSignalModel:
    """Forecast hypothesis: future fractional return = k * trailing momentum.

    Trailing multi-bar momentum is NOT by itself an estimated future return.
    k must be a documented/validated forecasting hypothesis. Threshold filters
    small indicators; passing it does not establish predictive significance.
    """

    feature_version: str
    model_version: str
    horizon_ms: int
    forecast_multiplier: Decimal
    min_abs_momentum: Decimal = Decimal("0")
    model_score: Decimal = Decimal("0.5")
    strategy_id: str = field(default="momentum", init=False)

    def __post_init__(self) -> None:
        _validate_model(
            feature_version=self.feature_version,
            model_version=self.model_version,
            horizon_ms=self.horizon_ms,
            forecast_multiplier=self.forecast_multiplier,
            min_abs_feature=self.min_abs_momentum,
            model_score=self.model_score,
        )

    def predict(
        self, snapshot: FeatureSnapshot, *, generated_at_ms: int,
    ) -> AlphaSignal | None:
        if not isinstance(snapshot, FeatureSnapshot):
            raise TypeError("expected FeatureSnapshot")
        _timestamp(generated_at_ms, "generated_at_ms")
        if snapshot.available_at_ms > generated_at_ms:
            raise ValueError("future feature in momentum model")
        if snapshot.feature_version != self.feature_version:
            raise ValueError("momentum model feature version mismatch")
        if "momentum" not in snapshot.values:
            raise ValueError("momentum feature is missing")
        observed = snapshot.values["momentum"]
        if abs(observed) <= self.min_abs_momentum:
            return None
        with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
            forecast = observed * self.forecast_multiplier
        if forecast == 0:
            return None
        return _make_signal(
            snapshot, generated_at_ms=generated_at_ms,
            strategy_id=self.strategy_id, model_version=self.model_version,
            horizon_ms=self.horizon_ms,
            expected_return=forecast, model_score=self.model_score,
            configuration={
                "feature_version": self.feature_version,
                "forecast_multiplier": str(self.forecast_multiplier),
                "min_abs_momentum": str(self.min_abs_momentum),
            },
        )


@dataclass(frozen=True, slots=True)
class MeanReversionSignalModel:
    """Forecast hypothesis: future fractional return = -k * zscore * vol.

    zscore is dimensionless; realized_vol is the observed per-bar fractional
    standard deviation. Their product gives an explicit return-scale proxy.
    This is NOT a fitted or statistically verified conditional expectation.
    """

    feature_version: str
    model_version: str
    horizon_ms: int
    forecast_multiplier: Decimal
    min_abs_zscore: Decimal = Decimal("0")
    model_score: Decimal = Decimal("0.5")
    strategy_id: str = field(default="mean_reversion", init=False)

    def __post_init__(self) -> None:
        _validate_model(
            feature_version=self.feature_version,
            model_version=self.model_version,
            horizon_ms=self.horizon_ms,
            forecast_multiplier=self.forecast_multiplier,
            min_abs_feature=self.min_abs_zscore,
            model_score=self.model_score,
        )

    def predict(
        self, snapshot: FeatureSnapshot, *, generated_at_ms: int,
    ) -> AlphaSignal | None:
        if not isinstance(snapshot, FeatureSnapshot):
            raise TypeError("expected FeatureSnapshot")
        _timestamp(generated_at_ms, "generated_at_ms")
        if snapshot.available_at_ms > generated_at_ms:
            raise ValueError("future feature in mean-reversion model")
        if snapshot.feature_version != self.feature_version:
            raise ValueError("reversion model feature version mismatch")
        if "price_zscore" not in snapshot.values or "realized_vol" not in snapshot.values:
            raise ValueError("mean-reversion model needs zscore and realized_vol")
        zscore = snapshot.values["price_zscore"]
        vol = snapshot.values["realized_vol"]
        if abs(zscore) <= self.min_abs_zscore or vol == 0:
            return None
        if vol < 0:
            raise ValueError("realized volatility cannot be negative")
        with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
            forecast = -(self.forecast_multiplier * zscore * vol)
        if forecast == 0:
            return None
        return _make_signal(
            snapshot, generated_at_ms=generated_at_ms,
            strategy_id=self.strategy_id, model_version=self.model_version,
            horizon_ms=self.horizon_ms,
            expected_return=forecast, model_score=self.model_score,
            configuration={
                "feature_version": self.feature_version,
                "forecast_multiplier": str(self.forecast_multiplier),
                "min_abs_zscore": str(self.min_abs_zscore),
            },
        )
