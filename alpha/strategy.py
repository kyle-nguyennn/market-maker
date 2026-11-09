"""Venue-independent distinction between signal models and trading strategies.

A SignalModel predicts a signed return from features. A Strategy consumes
validated features/signals plus optional market and portfolio state and
decides desired exposure or quote proposals. This module is deliberately
independent of market-maker's quoter, order, execution and venue classes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

from .domain import (
    AlphaSignal, FeatureSnapshot, TargetExposure,
    _decimal, _horizon, _name, _timestamp,
)


@runtime_checkable
class AlphaStrategy(Protocol):
    """Legacy P1 prediction interface, preserved for existing callers."""

    @property
    def strategy_id(self) -> str:
        ...

    def generate_signal(self, snapshot: FeatureSnapshot) -> AlphaSignal | None:
        ...


@runtime_checkable
class SignalModel(Protocol):
    """A clock-injected forecasting model used by SignalEngine."""

    @property
    def strategy_id(self) -> str:
        ...

    def predict(
        self, snapshot: FeatureSnapshot, *, generated_at_ms: int,
    ) -> AlphaSignal | None:
        ...


class MarketView(Protocol):
    """Minimal shared view structurally implemented by model.MarketState."""

    instrument: str
    timestamp_ms: int
    is_valid: bool


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """Read-only portfolio view, without importing venue/accounting packages."""

    as_of_ms: int
    equity: Decimal
    positions: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        _timestamp(self.as_of_ms, "portfolio as_of_ms")
        _decimal(self.equity, "equity")
        if self.equity <= 0:
            raise ValueError("equity must be strictly positive")
        if not isinstance(self.positions, Mapping):
            raise ValueError("positions must be a mapping")
        frozen = {}
        for symbol, amount in self.positions.items():
            _name(symbol, "position symbol")
            _decimal(amount, f"position {symbol}")
            frozen[symbol] = amount
        object.__setattr__(
            self, "positions", MappingProxyType(dict(sorted(frozen.items())))
        )


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """As-of aligned inputs supplied identically in replay and live operation."""

    symbol: str
    decision_at_ms: int
    features: FeatureSnapshot
    signals: tuple[AlphaSignal, ...]
    market: MarketView | None = None
    portfolio: PortfolioSnapshot | None = None

    def __post_init__(self) -> None:
        _name(self.symbol, "symbol")
        _timestamp(self.decision_at_ms, "decision_at_ms")
        if not isinstance(self.features, FeatureSnapshot):
            raise TypeError("features must be FeatureSnapshot")
        if self.features.symbol != self.symbol:
            raise ValueError("feature symbol mismatch")
        if self.features.available_at_ms > self.decision_at_ms:
            raise ValueError("future feature in strategy context")
        if not isinstance(self.signals, tuple):
            raise TypeError("signals must be a tuple")
        seen_ids = set()
        for signal in self.signals:
            if not isinstance(signal, AlphaSignal):
                raise TypeError("signals must contain AlphaSignal")
            if (
                signal.symbol != self.symbol
                or signal.snapshot_id != self.features.snapshot_id
                or signal.feature_available_at_ms != self.features.available_at_ms
                or signal.generated_at_ms > self.decision_at_ms
            ):
                raise ValueError("unmatched or future signal in strategy context")
            if signal.signal_id in seen_ids:
                raise ValueError("duplicate signal ID in strategy context")
            seen_ids.add(signal.signal_id)
        if self.market is not None:
            if self.market.instrument != self.symbol:
                raise ValueError("market symbol mismatch")
            _timestamp(self.market.timestamp_ms, "market timestamp")
            if self.market.timestamp_ms > self.decision_at_ms:
                raise ValueError("future market data in strategy context")
        if self.portfolio is not None:
            if not isinstance(self.portfolio, PortfolioSnapshot):
                raise TypeError("portfolio must be PortfolioSnapshot")
            if self.portfolio.as_of_ms > self.decision_at_ms:
                raise ValueError("future portfolio in strategy context")


@dataclass(frozen=True, slots=True)
class QuoteProposal:
    """Desired quote, not an order or permission to submit one."""

    symbol: str
    side: str  # "BID" or "ASK"; wire/venue conversion belongs downstream.
    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        _name(self.symbol, "symbol")
        if self.side not in ("BID", "ASK"):
            raise ValueError("quote side must be BID or ASK")
        _decimal(self.price, "quote price")
        _decimal(self.size, "quote size")
        if self.price <= 0 or self.size <= 0:
            raise ValueError("quote price and size must be positive")


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """One trading policy output; may contain zero actions when abstaining.

    Exposure and quote proposals are mutually exclusive in one decision.
    Risk/sizing capacity, venue quantization and dispatch are downstream.
    """

    strategy_id: str
    decision_at_ms: int
    exposures: tuple[TargetExposure, ...] = ()
    quotes: tuple[QuoteProposal, ...] = ()

    def __post_init__(self) -> None:
        _name(self.strategy_id, "strategy_id")
        _timestamp(self.decision_at_ms, "decision_at_ms")
        if not isinstance(self.exposures, tuple) or not isinstance(self.quotes, tuple):
            raise TypeError("decision payloads must be tuples")
        if self.exposures and self.quotes:
            raise ValueError("mixed target-exposure and quote decisions not supported")
        if any(not isinstance(item, TargetExposure) for item in self.exposures):
            raise TypeError("exposures must contain TargetExposure")
        if any(not isinstance(item, QuoteProposal) for item in self.quotes):
            raise TypeError("quotes must contain QuoteProposal")
        if any(item.as_of_ms != self.decision_at_ms for item in self.exposures):
            raise ValueError("target exposure decision timestamp mismatch")


class Strategy(ABC):
    """Base trading policy family; no venue execution or order dependencies.

    Directional and market-making strategies subclass this ABC, while
    prediction models implement the separate SignalModel Protocol.
    """

    strategy_id: str

    @abstractmethod
    def decide(self, context: StrategyContext) -> StrategyDecision:
        """Return desired target exposures or quote proposals (or abstain)."""
        raise NotImplementedError
