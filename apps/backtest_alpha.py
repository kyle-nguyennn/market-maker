"""Research-only command line replay for existing alpha policy families.

Usage with a versioned, normalized OHLCV CSV (no credentials):
  python -m apps.backtest_alpha --csv prices.csv --symbol AAPL --source local-v1

For live-account-free Alpaca *historical data reads*:
  python -m apps.backtest_alpha --alpaca --symbol AAPL \
    --start-utc 2026-10-01T00:00:00+00:00 \
    --end-utc 2026-10-10T00:00:00+00:00 \
    --source research-capture-v1 --assumed-publication-lag-ms 1500

No trade endpoints, paper-position writes, or authenticated orders are used.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from alpha import (
    AlphaPipeline, FeatureConfig, FeatureEngine, MeanReversionSignalModel,
    MeanReversionStrategy, MomentumSignalModel, MomentumStrategy,
    SignalEngine, Strategy, StrategyContext, StrategyDecision,
)
from backtest import BacktestConfig, BacktestResult, CausalBarBacktester
from market_handler.historical_bar_reader import read_price_bars_csv


class CashStrategy(Strategy):
    """Always abstains; cash-only control for the same feature replay path."""

    strategy_id = "cash"

    def decide(self, context: StrategyContext) -> StrategyDecision:
        return StrategyDecision(self.strategy_id, context.decision_at_ms)


def _as_ms(timestamp: str) -> int:
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValueError("start/end must be ISO 8601 datetimes with an offset") from exc
    if parsed.utcoffset() is None:
        raise ValueError("start/end require explicit UTC offsets")
    return int(parsed.timestamp() * 1000)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Offline, next-eligible-bar-open alpha strategy backtest"
    )
    source_group = p.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--csv", type=Path, help="Normalized OHLCV CSV path")
    source_group.add_argument(
        "--alpaca", action="store_true",
        help="Read-only Alpaca historical API (requires market-data credentials)",
    )
    p.add_argument("--symbol", required=True)
    p.add_argument("--source", required=True,
                   help="Explicit CSV dataset revision or Alpaca capture identifier")
    p.add_argument("--start-utc", help="Alpaca: inclusive ISO8601 start with offset")
    p.add_argument("--end-utc", help="Alpaca: exclusive ISO8601 end with offset")
    p.add_argument("--feed", choices=("iex", "sip"), default="iex")
    p.add_argument("--adjustment", choices=("raw", "split", "dividend", "all"),
                   default="raw")
    p.add_argument("--assumed-publication-lag-ms", type=int, default=1500)
    p.add_argument("--interval-ms", type=int, default=60000)
    p.add_argument("--lookback-bars", type=int, default=20)
    p.add_argument("--max-staleness-ms", type=int, default=120000)
    p.add_argument("--feature-version", default="price-v1")
    p.add_argument("--strategy", choices=("momentum", "mean_reversion", "cash"),
                   default="momentum")
    p.add_argument("--model-version", default="baseline-hypothesis-v1")
    p.add_argument("--forecast-horizon-ms", type=int, default=600000)
    p.add_argument("--forecast-multiplier", type=Decimal, default=Decimal("0.25"))
    p.add_argument("--min-abs-feature", type=Decimal, default=Decimal("0"))
    p.add_argument("--model-score", type=Decimal, default=Decimal("0.5"))
    p.add_argument("--target-weight", type=Decimal, default=Decimal("0.10"))
    p.add_argument("--min-edge", type=Decimal, default=Decimal("0.001"))
    p.add_argument("--initial-cash", type=Decimal, default=Decimal("100000"))
    p.add_argument("--fee-bps", type=Decimal, default=Decimal("1"))
    p.add_argument("--slippage-bps", type=Decimal, default=Decimal("2"))
    p.add_argument("--latency-ms", type=int, default=1000)
    p.add_argument("--order-ttl-ms", type=int, default=600000)
    p.add_argument("--quantity-step", type=Decimal, default=Decimal("0.000001"))
    p.add_argument("--max-abs-target-weight", type=Decimal, default=Decimal("1"))
    p.add_argument("--rebalance-threshold", type=Decimal, default=Decimal("0.001"))
    p.add_argument("--out-dir", type=Path, help="Optional equity and fill CSV export")
    return p


def _data(args: argparse.Namespace) -> tuple:
    if args.alpaca:
        if not args.start_utc or not args.end_utc:
            raise ValueError("Alpaca history requires --start-utc and --end-utc")
        from market_handler.alpaca_stock_market_data import AlpacaHistoricalBarProvider
        provider = AlpacaHistoricalBarProvider.from_environment(
            start_ms=_as_ms(args.start_utc),
            assumed_publication_lag_ms=args.assumed_publication_lag_ms,
            source_version=args.source, feed=args.feed, adjustment=args.adjustment,
        )
        bars = provider.get_bars(args.symbol, until_ms=_as_ms(args.end_utc))
    else:
        bars = read_price_bars_csv(
            args.csv, interval_ms=args.interval_ms, source=args.source,
        )
        if any(x.symbol != args.symbol for x in bars):
            raise ValueError("CSV contains bars for a different symbol")
    return tuple(bars)


def _pipeline(args: argparse.Namespace) -> AlphaPipeline:
    if args.strategy == "momentum":
        model = MomentumSignalModel(
            feature_version=args.feature_version,
            model_version=args.model_version,
            horizon_ms=args.forecast_horizon_ms,
            forecast_multiplier=args.forecast_multiplier,
            min_abs_momentum=args.min_abs_feature,
            model_score=args.model_score,
        )
        policy: Strategy = MomentumStrategy(
            target_weight=args.target_weight, min_edge=args.min_edge,
        )
    elif args.strategy == "mean_reversion":
        model = MeanReversionSignalModel(
            feature_version=args.feature_version,
            model_version=args.model_version,
            horizon_ms=args.forecast_horizon_ms,
            forecast_multiplier=args.forecast_multiplier,
            min_abs_zscore=args.min_abs_feature,
            model_score=args.model_score,
        )
        policy = MeanReversionStrategy(
            target_weight=args.target_weight, min_edge=args.min_edge,
        )
    else:
        model = None
        policy = CashStrategy()
    return AlphaPipeline(
        features=FeatureEngine(
            symbol=args.symbol,
            config=FeatureConfig(
                interval_ms=args.interval_ms, lookback_bars=args.lookback_bars,
                max_staleness_ms=args.max_staleness_ms,
                feature_version=args.feature_version,
            ),
        ),
        signals=SignalEngine([] if model is None else [model]),
        strategy=policy,
    )


def _write_csv(path: Path, records: Sequence[dict]) -> None:
    if not records:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0].keys()))
        writer.writeheader()
        for record in records:
            writer.writerow({k: str(v) for k, v in record.items()})


def _export(result: BacktestResult, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _write_csv(directory / "equity.csv", [
        dict(as_of_ms=p.as_of_ms, bar_end_ms=p.bar_end_ms,
             close_price=p.close_price, cash=p.cash,
             position_quantity=p.position_quantity, equity=p.equity)
        for p in result.equity_curve
    ])
    _write_csv(directory / "fills.csv", [
        dict(decision_id=f.decision_id, symbol=f.symbol,
             decision_at_ms=f.decision_at_ms, eligible_at_ms=f.eligible_at_ms,
             filled_at_ms=f.filled_at_ms, bar_open=f.bar_open,
             fill_price=f.fill_price, signed_quantity=f.signed_quantity,
             notional=f.notional, fee=f.fee,
             slippage_cost=f.slippage_cost,
             realized_price_pnl=f.realized_price_pnl,
             signal_ids="|".join(f.signal_ids))
        for f in result.fills
    ])


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.alpaca and args.interval_ms != 60000:
        raise ValueError("Alpaca SDK adapter currently supports only minute bars")
    bars = _data(args)
    simulator = CausalBarBacktester(
        pipeline=_pipeline(args),
        config=BacktestConfig(
            initial_cash=args.initial_cash, latency_ms=args.latency_ms,
            order_ttl_ms=args.order_ttl_ms, fee_bps=args.fee_bps,
            slippage_bps=args.slippage_bps,
            quantity_step=args.quantity_step,
            max_abs_target_weight=args.max_abs_target_weight,
            rebalance_threshold=args.rebalance_threshold,
        ),
    )
    result = simulator.run(bars)
    metrics = result.metrics
    report = {
        "symbol": result.symbol,
        "strategy": args.strategy,
        "source": args.source,
        "bars": len(bars),
        "decisions": metrics.generated_decisions,
        "fills": metrics.fills,
        "initial_equity": str(metrics.initial_equity),
        "final_equity": str(metrics.final_equity),
        "net_pnl": str(metrics.net_pnl),
        "net_return": str(metrics.net_return),
        "max_drawdown": str(metrics.max_drawdown),
        "total_fees": str(metrics.total_fees),
        "total_slippage_cost": str(metrics.total_slippage_cost),
        "turnover_initial_equity": str(metrics.turnover_initial_equity),
        "daily_sharpe": (
            str(metrics.daily_sharpe) if metrics.daily_sharpe is not None else None
        ),
        "expired_intents": result.expired_intents,
        "pending_end": result.outstanding_target is not None,
        "fill_assumption": "first_future_bar_open_after_signal_and_latency",
        "point_in_time_certified": False,
    }
    if args.out_dir:
        _export(result, args.out_dir)
        report["out_dir"] = str(args.out_dir)
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
