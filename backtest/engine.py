"""Deterministic, *offline* bar-event simulator for directional strategies.

This host-side package intentionally does not import exchange SDKs, venue
adapters, broker accounts or risk/production order managers. Strategies are
the exact alpha.Strategy implementations used in live-style ingestion.

Timing: each historical bar carries its opening timestamp, completion time
and modeled publication time. A decision made at bar publication is NEVER
eligible for that bar's open or an already-passed later bar open. A target is
filled only at the first future bar OPEN satisfying decision+latency and TTL.
The actual historical open price is learned during replay as a hypothetical
execution proxy; guaranteed market-open fills are not claimed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_DOWN, ROUND_HALF_EVEN, localcontext
from typing import Sequence
from zoneinfo import ZoneInfo

from alpha.domain import TargetExposure, _decimal, _horizon, _timestamp
from alpha.observations import PriceBar
from alpha.pipeline import AlphaPipeline, PipelineResult

_BPS = Decimal("10000")
_ZERO = Decimal("0")
_US_EASTERN = ZoneInfo("America/New_York")
_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_cash: Decimal = Decimal("100000")
    latency_ms: int = 1000
    order_ttl_ms: int = 600_000
    fee_bps: Decimal = Decimal("1")
    slippage_bps: Decimal = Decimal("2")
    quantity_step: Decimal = Decimal("0.000001")
    max_abs_target_weight: Decimal = Decimal("1")
    rebalance_threshold: Decimal = Decimal("0.001")

    def __post_init__(self) -> None:
        for name in (
            "initial_cash", "fee_bps", "slippage_bps", "quantity_step",
            "max_abs_target_weight", "rebalance_threshold",
        ):
            _decimal(getattr(self, name), name)
        if self.initial_cash <= 0 or self.quantity_step <= 0:
            raise ValueError("initial_cash and quantity_step must be positive")
        if self.fee_bps < 0 or not _ZERO <= self.slippage_bps < _BPS:
            raise ValueError("fee_bps must be nonnegative and slippage_bps in [0,10000)")
        if self.max_abs_target_weight <= 0:
            raise ValueError("max_abs_target_weight must be positive")
        if self.rebalance_threshold < 0:
            raise ValueError("rebalance_threshold must be nonnegative")
        _timestamp(self.latency_ms, "latency_ms")
        _horizon(self.order_ttl_ms, "order_ttl_ms")


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    decision_id: str
    signal_ids: tuple[str, ...]
    symbol: str
    decision_at_ms: int
    eligible_at_ms: int
    filled_at_ms: int
    bar_open: Decimal
    fill_price: Decimal
    signed_quantity: Decimal
    notional: Decimal
    fee: Decimal
    slippage_cost: Decimal
    realized_price_pnl: Decimal


@dataclass(frozen=True, slots=True)
class EquityPoint:
    as_of_ms: int
    bar_end_ms: int
    close_price: Decimal
    cash: Decimal
    position_quantity: Decimal
    equity: Decimal


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    initial_equity: Decimal
    final_equity: Decimal
    net_pnl: Decimal
    net_return: Decimal
    max_drawdown: Decimal
    total_fees: Decimal
    total_slippage_cost: Decimal
    traded_notional: Decimal
    turnover_initial_equity: Decimal
    realized_price_pnl: Decimal
    fills: int
    generated_decisions: int
    daily_sharpe: Decimal | None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    symbol: str
    config: BacktestConfig
    fills: tuple[SimulatedFill, ...]
    equity_curve: tuple[EquityPoint, ...]
    signals_and_decisions: tuple[PipelineResult, ...]
    metrics: BacktestMetrics
    expired_intents: int
    outstanding_target: TargetExposure | None


@dataclass(frozen=True, slots=True)
class _PendingTarget:
    target: TargetExposure
    eligible_at_ms: int
    expires_at_ms: int


class CausalBarBacktester:
    """Single-asset simulated exposure execution using unchanged AlphaPipeline.

    It must be given a fresh AlphaPipeline and sorted, complete normalized bars.
    Futures are *not* accessed by features; future bar opens are used only for
    hypothetical execution once their event time has arrived.
    """

    def __init__(self, *, pipeline: AlphaPipeline, config: BacktestConfig) -> None:
        if not isinstance(pipeline, AlphaPipeline):
            raise TypeError("pipeline must be AlphaPipeline")
        if not isinstance(config, BacktestConfig):
            raise TypeError("config must be BacktestConfig")
        self.pipeline = pipeline
        self.config = config
        self._used = False
        self._cash = config.initial_cash
        self._quantity = _ZERO
        self._average_entry = _ZERO
        self._realized_price_pnl = _ZERO
        self._fees = _ZERO
        self._slippage = _ZERO
        self._traded_notional = _ZERO
        self._pending: _PendingTarget | None = None
        self._expired = 0
        self._fills: list[SimulatedFill] = []
        self._curve: list[EquityPoint] = []
        self._decisions: list[PipelineResult] = []

    def _mark(self, price: Decimal) -> Decimal:
        return self._cash + self._quantity * price

    def _apply_position(self, signed_qty: Decimal, fill_price: Decimal) -> Decimal:
        """Update signed average-cost ledger and return realized *price* P&L."""
        old = self._quantity
        previous_cost = self._average_entry
        closed_qty = (
            min(abs(old), abs(signed_qty))
            if old != 0 and (old > 0) != (signed_qty > 0)
            else _ZERO
        )
        realized = (
            closed_qty * (fill_price - previous_cost) * (1 if old > 0 else -1)
        )
        updated = old + signed_qty
        if updated == 0:
            self._average_entry = _ZERO
        elif old == 0 or (old > 0) != (updated > 0):
            self._average_entry = fill_price
        elif (old > 0) == (signed_qty > 0):
            # Position increased in the existing direction.
            self._average_entry = (
                abs(old) * previous_cost + abs(signed_qty) * fill_price
            ) / abs(updated)
        # Reductions without a side flip retain the old average cost.
        self._quantity = updated
        self._realized_price_pnl += realized
        return realized

    def _process_bar_open(self, bar: PriceBar) -> None:
        pending = self._pending
        if pending is None:
            return
        open_ms = bar.end_ms - bar.interval_ms
        if open_ms > pending.expires_at_ms:
            self._pending = None
            self._expired += 1
            return
        if open_ms < pending.eligible_at_ms:
            return
        if open_ms < pending.target.as_of_ms:
            raise AssertionError("a fill must not predate its strategy decision")

        equity_before = self._mark(bar.open)
        if equity_before <= 0:
            raise ValueError("nonpositive equity before simulated fill")
        # Pre-fill portfolio is marked at *this* bar's opening price.
        target_weight = pending.target.target_weight
        current_weight = self._quantity * bar.open / equity_before
        if abs(target_weight - current_weight) <= self.config.rebalance_threshold:
            self._pending = None
            return

        # First approximate target quantity from the reference open; then
        # account for adverse slippage when computing executed quantity.
        expected_qty = target_weight * equity_before / bar.open
        direction = expected_qty - self._quantity
        if direction == 0:
            self._pending = None
            return
        sign = 1 if direction > 0 else -1
        fill_price = bar.open * (
            Decimal("1") + sign * self.config.slippage_bps / _BPS
        )
        target_qty = (
            (target_weight * equity_before / fill_price)
            / self.config.quantity_step
        ).to_integral_value(rounding=ROUND_DOWN) * self.config.quantity_step
        delta = target_qty - self._quantity
        if delta == 0:
            self._pending = None
            return

        # A large slippage can in principle flip the desired side; do not
        # trade contrary to the reference-price targeting intent.
        if (delta > 0) != (direction > 0):
            self._pending = None
            return
        notional = abs(delta) * fill_price
        fee = notional * self.config.fee_bps / _BPS
        slip_cost = abs(delta) * abs(fill_price - bar.open)
        self._cash -= delta * fill_price + fee
        realized = self._apply_position(delta, fill_price)
        self._fees += fee
        self._slippage += slip_cost
        self._traded_notional += notional
        self._fills.append(SimulatedFill(
            decision_id=pending.target.decision_id,
            signal_ids=pending.target.signal_ids,
            symbol=bar.symbol,
            decision_at_ms=pending.target.as_of_ms,
            eligible_at_ms=pending.eligible_at_ms,
            filled_at_ms=open_ms,
            bar_open=bar.open,
            fill_price=fill_price,
            signed_quantity=delta,
            notional=notional,
            fee=fee,
            slippage_cost=slip_cost,
            realized_price_pnl=realized,
        ))
        self._pending = None

    def _handle_decision(self, result: PipelineResult) -> None:
        decision = result.decision
        if decision.quotes:
            raise ValueError(
                "bar backtester cannot simulate maker quotes; "
                "use order-book event replay with an explicit fill model"
            )
        if len(decision.exposures) > 1:
            raise ValueError("single-asset simulator requires at most one exposure")
        if not decision.exposures:
            return  # abstention is NOT implicit flatten or cancel
        target = decision.exposures[0]
        if target.symbol != self.pipeline.features.symbol:
            raise ValueError("target instrument mismatch")
        if abs(target.target_weight) > self.config.max_abs_target_weight:
            raise ValueError("target exceeds configured gross weight limit")
        if self._pending is not None and (
            self._pending.target.target_weight == target.target_weight
        ):
            # Keep the first submitted intent's eligibility / TTL. Repeated
            # identical strategy decisions must not starve fills by endlessly
            # resetting the latency clock.
            return

        eligible = target.as_of_ms + self.config.latency_ms
        expires = min(
            target.as_of_ms + self.config.order_ttl_ms,
            target.as_of_ms + target.horizon_ms,
        )
        if expires < eligible:
            raise ValueError("forecast horizon/order TTL shorter than execution latency")
        self._pending = _PendingTarget(target, eligible, expires)

    def run(self, bars: Sequence[PriceBar]) -> BacktestResult:
        if self._used:
            raise RuntimeError("backtester is single-use; construct a fresh pipeline")
        self._used = True
        if not bars:
            raise ValueError("at least one completed price bar is required")
        symbol = self.pipeline.features.symbol
        previous_end: int | None = None
        previous_publication: int | None = None
        with localcontext(_CONTEXT):
            for bar in bars:
                if not isinstance(bar, PriceBar):
                    raise TypeError("all observations must be normalized PriceBar")
                if bar.symbol != symbol:
                    raise ValueError("mixed symbols in single-asset backtest")
                if previous_end is not None and bar.end_ms <= previous_end:
                    raise ValueError("bars must have strictly increasing completion times")
                if (previous_publication is not None
                        and bar.available_at_ms < previous_publication):
                    raise ValueError("bar publication timestamps must be monotonic")
                previous_end = bar.end_ms
                previous_publication = bar.available_at_ms
                self._process_bar_open(bar)
                equity = self._mark(bar.close)
                if equity <= 0:
                    raise ValueError("nonpositive account equity after mark-to-market")
                result = self.pipeline.on_bar(bar, at_ms=bar.available_at_ms)
                if result is not None:
                    self._decisions.append(result)
                    self._handle_decision(result)
                self._curve.append(EquityPoint(
                    as_of_ms=bar.available_at_ms,
                    bar_end_ms=bar.end_ms,
                    close_price=bar.close,
                    cash=self._cash,
                    position_quantity=self._quantity,
                    equity=equity,
                ))
            metrics = _metrics(
                config=self.config,
                points=self._curve,
                fills=self._fills,
                fees=self._fees, slippage=self._slippage,
                traded_notional=self._traded_notional,
                realized_price_pnl=self._realized_price_pnl,
                decision_count=len(self._decisions),
            )
            return BacktestResult(
                symbol=symbol, config=self.config,
                fills=tuple(self._fills),
                equity_curve=tuple(self._curve),
                signals_and_decisions=tuple(self._decisions),
                metrics=metrics, expired_intents=self._expired,
                outstanding_target=(
                    self._pending.target if self._pending is not None else None
                ),
            )


def _metrics(
    *, config: BacktestConfig, points: Sequence[EquityPoint],
    fills: Sequence[SimulatedFill], fees: Decimal,
    slippage: Decimal, traded_notional: Decimal,
    realized_price_pnl: Decimal, decision_count: int,
) -> BacktestMetrics:
    with localcontext(_CONTEXT):
        initial = config.initial_cash
        final = points[-1].equity
        pnl = final - initial
        peak = initial
        max_dd = _ZERO
        daily_last: dict[str, Decimal] = {}
        for point in points:
            peak = max(peak, point.equity)
            max_dd = max(max_dd, (peak - point.equity) / peak)
            # US equity sessions may shift UTC hour due to DST.
            day = datetime.fromtimestamp(
                point.bar_end_ms / 1000, tz=timezone.utc
            ).astimezone(_US_EASTERN).date().isoformat()
            daily_last[day] = point.equity

        returns = []
        previous = initial
        for daily_equity in daily_last.values():
            returns.append(daily_equity / previous - Decimal("1"))
            previous = daily_equity

        sharpe: Decimal | None = None
        if len(returns) >= 2:
            n = Decimal(len(returns))
            mean = sum(returns, _ZERO) / n
            variance = sum(
                ((v - mean) ** 2 for v in returns), _ZERO
            ) / (n - Decimal("1"))
            if variance > 0:
                sharpe = mean / variance.sqrt() * Decimal("252").sqrt()

        return BacktestMetrics(
            initial_equity=initial, final_equity=final,
            net_pnl=pnl, net_return=pnl / initial,
            max_drawdown=max_dd,
            total_fees=fees, total_slippage_cost=slippage,
            traded_notional=traded_notional,
            turnover_initial_equity=traded_notional / initial,
            realized_price_pnl=realized_price_pnl,
            fills=len(fills), generated_decisions=decision_count,
            daily_sharpe=sharpe,
        )
