from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from model.market import MarketState
from model.order import OrderIntent, Side


@dataclass(frozen=True)
class RiskLimits:
    """All monetary limits are in the DEX collateral currency.

    max_drawdown is an ABSOLUTE currency amount (not a percentage).
    Fields without validated thresholds default to None and block new orders.
    """
    max_abs_position: Decimal
    max_order_notional: Decimal
    max_abs_notional: Decimal
    max_drawdown: Decimal
    max_market_data_age_ms: int
    min_order_notional: Decimal | None = None
    max_gross_notional: Decimal | None = None
    max_net_notional: Decimal | None = None
    max_quote_deviation_bps: Decimal | None = None


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = "ok"


@dataclass(frozen=True)
class OpenExposure:
    """One still-fillable order (INCLUDING pending new and pending cancel)."""
    instrument: str
    side: Side
    remaining_size: Decimal
    client_order_id: str


@dataclass(frozen=True)
class RiskSnapshot:
    """Verified venue-neutral read model, produced by an account/data adapter.

    A valid margin_mode plus fresh account_equity and side capacities are
    required. Capacity fields are quantities; the caller must map confirmed
    exchange availableToTrade/maxTradeSzs responses to these quantities.
    """
    market: MarketState
    now_ms: int
    dex: str
    expected_dex: str
    allowed_instruments: frozenset[str]
    positions: Mapping[str, Decimal]
    marks: Mapping[str, Decimal]
    fillable_orders: tuple[OpenExposure, ...]
    account_equity: Decimal | None
    baseline_equity: Decimal | None
    margin_mode: str | None
    capacity_buy: Mapping[str, Decimal] | None
    capacity_sell: Mapping[str, Decimal] | None
    account_id: str | None = None
    expected_account_id: str | None = None


class RiskManager:
    """Fail-closed pre-send risk rules; no venue SDK or strategy dependencies."""

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self.kill_switch = False
        self._validate_limits()

    @staticmethod
    def _nonnegative(x: Decimal | None) -> bool:
        return isinstance(x, Decimal) and x.is_finite() and x >= 0

    @classmethod
    def _positive(cls, x: Decimal | None) -> bool:
        return cls._nonnegative(x) and x > 0

    def _validate_limits(self) -> None:
        for name in ("max_abs_position", "max_order_notional",
                     "max_abs_notional", "max_drawdown"):
            value = getattr(self.limits, name)
            if value is not None and not self._positive(value):
                raise ValueError(f"{name} must be a positive finite Decimal")
        if not isinstance(self.limits.max_market_data_age_ms, int) or self.limits.max_market_data_age_ms <= 0:
            raise ValueError("max_market_data_age_ms must be positive")
        for name in ("min_order_notional", "max_gross_notional",
                     "max_net_notional", "max_quote_deviation_bps"):
            value = getattr(self.limits, name)
            if value is not None and not self._positive(value):
                raise ValueError(f"{name} must be a positive finite Decimal")

    def evaluate_market_age(self, age_ms: int) -> RiskDecision:
        if self.kill_switch:
            return RiskDecision(False, "kill_switch")
        if age_ms < 0:
            return RiskDecision(False, "future_market_data")
        if age_ms > self.limits.max_market_data_age_ms:
            return RiskDecision(False, "stale_market_data")
        return RiskDecision(True)

    def evaluate_order_notional(self, price: Decimal, size: Decimal) -> RiskDecision:
        if self.kill_switch:
            return RiskDecision(False, "kill_switch")
        if not self._positive(price) or not self._positive(size):
            return RiskDecision(False, "invalid_order_price_or_size")
        if self.limits.max_order_notional is None:
            return RiskDecision(False, "risk_config_incomplete")
        if price * size > self.limits.max_order_notional:
            return RiskDecision(False, "max_order_notional")
        return RiskDecision(True)

    def trigger_kill_switch(self) -> None:
        self.kill_switch = True

    def reset_kill_switch(self, *, operator_authorized: bool, state_reconciled: bool) -> None:
        if not operator_authorized or not state_reconciled:
            raise PermissionError("explicit operator authorization and clean reconciliation required")
        self.kill_switch = False

    def evaluate_placement(self, intent: OrderIntent, snap: RiskSnapshot) -> RiskDecision:
        """Evaluate the FINAL adapter-quantized proposal immediately before send.

        All resting, pending-new and pending-cancel orders must be in fillable_orders.
        If the proposal is already reserved by OrderManager, exclude its CLOID
        once before adding it back to worst-case exposure.
        """
        if self.kill_switch:
            return RiskDecision(False, "kill_switch")
        l = self.limits
        if any(getattr(l, key) is None for key in (
            "max_abs_position", "max_order_notional", "max_abs_notional",
            "max_drawdown", "min_order_notional", "max_gross_notional",
            "max_net_notional", "max_quote_deviation_bps",
        )):
            return RiskDecision(False, "risk_config_incomplete")
        if snap.dex != snap.expected_dex:
            return RiskDecision(False, "wrong_dex")
        if (not snap.account_id or not snap.expected_account_id
            or snap.account_id.lower() != snap.expected_account_id.lower()):
            return RiskDecision(False, "wrong_or_unverified_account")
        if (intent.instrument not in snap.allowed_instruments
            or intent.instrument != snap.market.instrument
            or ":" in intent.instrument):
            return RiskDecision(False, "market_not_allowed")
        if not isinstance(intent.side, Side) or not intent.post_only:
            return RiskDecision(False, "unsupported_order_type")
        base = self.evaluate_order_notional(intent.price, intent.size)
        if not base.allowed:
            return base
        market = snap.market
        if not market.is_valid:
            return RiskDecision(False, market.invalid_reason or "invalid_book")
        if not isinstance(snap.now_ms, int) or not isinstance(market.timestamp_ms, int):
            return RiskDecision(False, "invalid_market_timestamp")
        age = self.evaluate_market_age(snap.now_ms - market.timestamp_ms)
        if not age.allowed:
            return age
        bid, ask = market.best_bid, market.best_ask
        if not self._positive(bid) or not self._positive(ask) or bid >= ask:
            return RiskDecision(False, "invalid_book")
        # Strict non-marketable ALO, checked AFTER venue price rounding.
        if (intent.side is Side.BID and intent.price >= ask) or (
            intent.side is Side.ASK and intent.price <= bid
        ):
            return RiskDecision(False, "post_only_would_cross")
        mid = (bid + ask) / Decimal("2")
        deviation_bps = abs(intent.price - mid) / mid * Decimal("10000")
        if deviation_bps > l.max_quote_deviation_bps:
            return RiskDecision(False, "price_collar")
        notional = intent.price * intent.size
        position = snap.positions.get(intent.instrument)
        if not self._nonnegative(snap.account_equity) or not self._nonnegative(snap.baseline_equity):
            return RiskDecision(False, "unverified_account_equity")
        if snap.margin_mode not in ("standard", "unified"):
            return RiskDecision(False, "unknown_margin_mode")
        if snap.baseline_equity - snap.account_equity > l.max_drawdown:
            self.trigger_kill_switch()
            return RiskDecision(False, "drawdown_kill_switch")
        if not isinstance(position, Decimal) or not position.is_finite():
            return RiskDecision(False, "unverified_position")
        if snap.capacity_buy is None or snap.capacity_sell is None:
            return RiskDecision(False, "capacity_unavailable")
        capacity = (snap.capacity_buy if intent.side is Side.BID else snap.capacity_sell).get(intent.instrument)
        if not self._nonnegative(capacity):
            return RiskDecision(False, "capacity_unavailable")
        if intent.size > capacity:
            return RiskDecision(False, "insufficient_capacity")

        signed = Decimal("1") if intent.side is Side.BID else Decimal("-1")
        if intent.reduce_only:
            if position * signed >= 0 or intent.size > abs(position):
                return RiskDecision(False, "invalid_reduce_only")
        if notional < l.min_order_notional:
            # Exception only for a full-position, reduce-only close.
            if not (intent.reduce_only and position * signed < 0 and intent.size == abs(position)):
                return RiskDecision(False, "min_order_notional")

        # Aggregate independent best/worst all-fill scenarios. A cancel request
        # does NOT remove risk until its order is confirmed terminal.
        bounds: dict[str, list[Decimal]] = {
            symbol: [qty, qty] for symbol, qty in snap.positions.items()
        }
        for symbol, qty in snap.positions.items():
            if not isinstance(qty, Decimal) or not qty.is_finite():
                return RiskDecision(False, "unverified_position")
        for exposure in snap.fillable_orders:
            if exposure.client_order_id == intent.client_order_id:
                continue  # Reserved proposal must not be counted twice.
            if not self._nonnegative(exposure.remaining_size) or not isinstance(exposure.side, Side):
                return RiskDecision(False, "invalid_open_exposure")
            if exposure.instrument not in bounds:
                return RiskDecision(False, "unverified_position")
            lo, hi = bounds[exposure.instrument]
            if exposure.side is Side.BID:
                hi += exposure.remaining_size
            else:
                lo -= exposure.remaining_size
            bounds[exposure.instrument] = [lo, hi]

        lo, hi = bounds[intent.instrument]
        if intent.side is Side.BID:
            hi += intent.size
        else:
            lo -= intent.size
        bounds[intent.instrument] = [lo, hi]
        if max(abs(lo), abs(hi)) > l.max_abs_position:
            return RiskDecision(False, "max_abs_position")
        gross = Decimal(0)
        net_lo = Decimal(0)
        net_hi = Decimal(0)
        for symbol, (low, high) in bounds.items():
            mark = snap.marks.get(symbol)
            if not self._positive(mark):
                return RiskDecision(False, "missing_mark_price")
            if symbol == intent.instrument and max(abs(low * mark), abs(high * mark)) > l.max_abs_notional:
                return RiskDecision(False, "max_abs_notional")
            gross += max(abs(low), abs(high)) * mark
            net_lo += low * mark
            net_hi += high * mark
        if gross > l.max_gross_notional:
            return RiskDecision(False, "max_gross_notional")
        if max(abs(net_lo), abs(net_hi)) > l.max_net_notional:
            return RiskDecision(False, "max_net_notional")
        return RiskDecision(True)
