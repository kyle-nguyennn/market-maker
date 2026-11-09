"""Venue-independent, immutable alpha research contracts.

These objects are not orders, fills, or permission to trade. All numerical
model outputs and features use finite Decimal values; venue adapters own any
tick/precision conversion.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping


def _name(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a non-empty trimmed string")


def _timestamp(value: int, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer in milliseconds")


def _horizon(value: int, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer in milliseconds")


def _decimal(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{label} must be a finite Decimal")


def _confidence(value: Decimal) -> None:
    _decimal(value, "confidence")
    if not Decimal("0") <= value <= Decimal("1"):
        raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """Features *actually available* at a particular instant.

    observed_at_ms is the newest underlying observation timestamp;
    available_at_ms is when the complete derived feature vector became
    usable (including acquisition, publication and computation delay).
    A consumer must not generate a decision before available_at_ms.
    lookback_ms describes the input observation window, not forecast horizon.

    Copy and freeze values to prevent upstream cache mutations changing a
    previously recorded research observation.
    """

    snapshot_id: str
    symbol: str
    observed_at_ms: int
    available_at_ms: int
    lookback_ms: int
    source: str
    feature_version: str
    values: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        for label in ("snapshot_id", "symbol", "source", "feature_version"):
            _name(getattr(self, label), label)
        _timestamp(self.observed_at_ms, "observed_at_ms")
        _timestamp(self.available_at_ms, "available_at_ms")
        _horizon(self.lookback_ms, "lookback_ms")
        if self.available_at_ms < self.observed_at_ms:
            raise ValueError("features cannot be available before their newest observation")
        if not isinstance(self.values, Mapping) or not self.values:
            raise ValueError("values must be a non-empty feature mapping")
        frozen = {}
        for key, value in self.values.items():
            _name(key, "feature name")
            _decimal(value, f"feature {key}")
            frozen[key] = value
        object.__setattr__(self, "values", MappingProxyType(dict(sorted(frozen.items()))))


@dataclass(frozen=True, slots=True)
class AlphaSignal:
    """Signed return forecast on a declared horizon; never a trade command.

    expected_return is a dimensionless fractional return (0.002 means 0.2%),
    NOT a price and NOT a desired portfolio weight. Negative means bearish.
    Confidence is a [0, 1] model score, not necessarily a calibrated win rate.
    feature_available_at_ms preserves no-lookahead lineage at construction.
    """

    signal_id: str
    snapshot_id: str
    symbol: str
    strategy: str
    model_version: str
    generated_at_ms: int
    feature_available_at_ms: int
    horizon_ms: int
    expected_return: Decimal
    confidence: Decimal
    regime: str | None = None

    def __post_init__(self) -> None:
        for label in ("signal_id", "snapshot_id", "symbol", "strategy", "model_version"):
            _name(getattr(self, label), label)
        if self.regime is not None:
            _name(self.regime, "regime")
        _timestamp(self.generated_at_ms, "generated_at_ms")
        _timestamp(self.feature_available_at_ms, "feature_available_at_ms")
        _horizon(self.horizon_ms, "horizon_ms")
        if self.generated_at_ms < self.feature_available_at_ms:
            raise ValueError("signal cannot predate availability of its input features")
        _decimal(self.expected_return, "expected_return")
        _confidence(self.confidence)


@dataclass(frozen=True, slots=True)
class TargetExposure:
    """Desired account-equity allocation selected downstream of alpha signals.

    Signed target_weight is a *fraction of account equity* (0.15 means
    15% long; -0.10 means 10% short; zero means flat). It is NOT a venue
    position size or an order. This pure domain model does not enforce
    leverage, gross exposure, concentration, trade count or venue limits:
    those are portfolio/risk/execution policies applied before sending.

    expected_return and confidence describe the supporting forecast, not
    guarantees of execution or profitability. signal_ids are lineage
    references; an ensemble may cite multiple signals.
    """

    decision_id: str
    symbol: str
    as_of_ms: int
    horizon_ms: int
    target_weight: Decimal
    expected_return: Decimal
    confidence: Decimal
    strategy: str
    signal_ids: tuple[str, ...]
    regime: str | None = None

    def __post_init__(self) -> None:
        for label in ("decision_id", "symbol", "strategy"):
            _name(getattr(self, label), label)
        if self.regime is not None:
            _name(self.regime, "regime")
        _timestamp(self.as_of_ms, "as_of_ms")
        _horizon(self.horizon_ms, "horizon_ms")
        _decimal(self.target_weight, "target_weight")
        _decimal(self.expected_return, "expected_return")
        _confidence(self.confidence)
        if not isinstance(self.signal_ids, tuple) or not self.signal_ids:
            raise ValueError("signal_ids must be a non-empty tuple")
        for signal_id in self.signal_ids:
            _name(signal_id, "signal id")
        if len(set(self.signal_ids)) != len(self.signal_ids):
            raise ValueError("signal_ids must not contain duplicates")
