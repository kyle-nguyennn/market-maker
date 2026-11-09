"""Deterministic causal fill, ledger and cost tests. No network or broker."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from alpha import (
    AlphaPipeline, FeatureConfig, FeatureEngine, MomentumSignalModel,
    MeanReversionSignalModel, MomentumStrategy, MeanReversionStrategy,
    PriceBar, QuoteProposal, SignalEngine, Strategy, StrategyDecision,
    TargetExposure,
)
from backtest import BacktestConfig, CausalBarBacktester

MINUTE = 60_000


def bars(prices=None, *, lag_ms=1000, start_i=1):
    if prices is None:
        prices = ["101", "102", "103", "104", "105", "106", "107", "108"]
    result = []
    for i, price in enumerate(prices, start=start_i):
        p = D(price)
        result.append(PriceBar(
            symbol="AAPL", source="synthetic-raw-v1",
            end_ms=i * MINUTE,
            available_at_ms=i * MINUTE + lag_ms,
            interval_ms=MINUTE,
            open=p, high=p, low=p, close=p, volume=D("500"),
        ))
    return tuple(result)


def pipeline(*, side="momentum", weight="0.5", horizon_ms=600_000):
    if side == "momentum":
        model = MomentumSignalModel(
            feature_version="price-v1", model_version="fixed-momentum",
            horizon_ms=horizon_ms, forecast_multiplier=D("0.25"),
        )
        strategy = MomentumStrategy(target_weight=D(weight))
    elif side == "reversion":
        model = MeanReversionSignalModel(
            feature_version="price-v1", model_version="fixed-reversion",
            horizon_ms=horizon_ms, forecast_multiplier=D("0.5"),
        )
        strategy = MeanReversionStrategy(target_weight=D(weight))
    elif side == "abstain":
        model = None

        class Abstain(Strategy):
            strategy_id = "cash"
            def decide(self, ctx):
                return StrategyDecision("cash", ctx.decision_at_ms)
        strategy = Abstain()
    else:
        raise AssertionError("unknown test family")

    return AlphaPipeline(
        features=FeatureEngine(
            symbol="AAPL",
            config=FeatureConfig(
                interval_ms=MINUTE, lookback_bars=3,
                max_staleness_ms=2000, feature_version="price-v1",
            ),
        ),
        signals=SignalEngine([model] if model else []),
        strategy=strategy,
    )


def run(bars_input=None, *, side="momentum", config=None,
        weight="0.5", horizon_ms=600_000):
    return CausalBarBacktester(
        pipeline=pipeline(side=side, weight=weight, horizon_ms=horizon_ms),
        config=BacktestConfig(
            fee_bps=D("1"), slippage_bps=D("2"),
            quantity_step=D("0.000001"),
        ) if config is None else config,
    ).run(bars() if bars_input is None else bars_input)


def test_signal_cannot_fill_current_or_already_open_next_bar():
    result = run(bars()[:6])
    assert result.metrics.generated_decisions == 3  # bars 4,5,6
    assert result.fills
    first = result.fills[0]
    assert first.decision_at_ms == 4 * MINUTE + 1000
    assert first.eligible_at_ms == first.decision_at_ms + 1000
    # At decision bar 4 publication, bar 5 has already opened; bar 6 is
    # the first future open strictly AFTER the modeled signal arrival.
    assert first.filled_at_ms == 5 * MINUTE
    assert first.filled_at_ms >= first.eligible_at_ms
    assert first.filled_at_ms > first.decision_at_ms
    assert first.fill_price > first.bar_open  # adverse buy slippage
    assert first.signed_quantity > 0


def test_fees_slippage_cash_and_mark_to_market_exact_identity():
    r = run(bars()[:6])
    first = r.fills[0]
    last = r.equity_curve[-1]
    assert first.fee == first.notional * D("1") / D("10000")
    assert first.slippage_cost == (
        first.signed_quantity * (first.fill_price - first.bar_open)
    )
    assert last.cash == D("100000") - first.signed_quantity * first.fill_price - first.fee
    assert last.position_quantity == first.signed_quantity
    assert last.equity == last.cash + last.position_quantity * last.close_price
    assert r.metrics.net_pnl == last.equity - D("100000")
    assert r.metrics.total_fees == first.fee
    assert r.metrics.total_slippage_cost == first.slippage_cost
    assert r.metrics.traded_notional == first.notional


def test_no_trade_benchmark_is_flat_across_any_prices():
    r = run(side="abstain")
    assert not r.fills
    assert r.metrics.net_pnl == D("0")
    assert r.metrics.net_return == D("0")
    assert r.metrics.max_drawdown == D("0")
    assert r.metrics.daily_sharpe is None
    assert all(x.equity == D("100000") for x in r.equity_curve)


def test_reversion_shorts_rising_market_and_loses_when_price_keeps_rising():
    r = run(bars()[:8], side="reversion")
    assert r.fills
    assert r.fills[0].signed_quantity < 0
    assert r.fills[0].fill_price < r.fills[0].bar_open
    assert r.metrics.net_pnl < 0
    assert r.metrics.max_drawdown > 0


def test_execution_is_sensitive_to_fees_and_slippage():
    observations = bars()[:7]
    free = run(observations, config=BacktestConfig(
        fee_bps=D("0"), slippage_bps=D("0"),
    ))
    costly = run(observations, config=BacktestConfig(
        fee_bps=D("10"), slippage_bps=D("20"),
    ))
    assert free.metrics.net_pnl > costly.metrics.net_pnl
    assert free.metrics.total_fees == D("0")
    assert free.metrics.total_slippage_cost == D("0")
    assert costly.metrics.total_fees > 0
    assert costly.metrics.total_slippage_cost > 0
    assert costly.fills[0].fill_price > free.fills[0].fill_price


def test_stale_intent_expires_instead_of_filling_from_past():
    observations = bars()[:7]
    result = run(observations, config=BacktestConfig(
        latency_ms=1000, order_ttl_ms=20_000,
    ))
    assert result.fills == ()
    assert result.expired_intents >= 1
    assert result.metrics.net_pnl == 0


def test_identical_pending_target_does_not_reset_latency_and_starve_fill():
    # With a 2-minute latency, publication of multiple identical signals
    # precedes the first eligible opening. Only the *first* target's clock
    # must govern eligibility.
    result = run(bars(), config=BacktestConfig(latency_ms=120_000))
    assert result.fills
    assert result.fills[0].decision_at_ms == 4 * MINUTE + 1000
    assert result.fills[0].eligible_at_ms == 6 * MINUTE + 1000
    assert result.fills[0].filled_at_ms == 7 * MINUTE


def test_unpublished_future_close_cannot_affect_historical_snapshots():
    original = bars()
    # Change only bar7 *close*. Its opening at 360000 is already observed
    # before bar6 publication at 361000, but its close remains unavailable
    # until 421000. Earlier snapshots and marks must be invariant.
    changed = original[:6] + (
        replace(original[6], high=D("900"), close=D("900")),
    ) + original[7:]
    before = run(original)
    after = run(changed)
    assert before.fills[0] == after.fills[0]
    assert before.equity_curve[:6] == after.equity_curve[:6]
    assert before.signals_and_decisions[:3] == after.signals_and_decisions[:3]


def test_publication_mark_uses_later_observed_open_not_stale_close():
    observations = bars()
    # bar7 opens at 360000 but bar6 close publishes at 361000.
    # At 361000 the actual bar7 open is a fresher observed price than
    # bar6 close; using bar6 close would regress the equity mark.
    changed = observations[:6] + (
        replace(observations[6], open=D("900"), high=D("900"),
                low=D("107"), close=D("900")),
    ) + observations[7:]
    base = run(observations)
    changed_run = run(changed)
    assert base.equity_curve[5].close_price == D("106")
    assert changed_run.equity_curve[5].close_price == D("106")
    assert base.equity_curve[5].mark_price == D("107")
    assert changed_run.equity_curve[5].mark_price == D("900")
    assert base.equity_curve[5].equity != changed_run.equity_curve[5].equity


def test_target_rejects_leverage_above_explicit_cap():
    with pytest.raises(ValueError, match="gross weight limit"):
        run(weight="1.5", config=BacktestConfig(max_abs_target_weight=D("1")))


def test_maker_quote_backtesting_fails_closed_without_l2_fill_model():
    class FakeMaker(Strategy):
        strategy_id = "maker"
        def decide(self, context):
            return StrategyDecision(
                self.strategy_id, context.decision_at_ms,
                quotes=(QuoteProposal(context.symbol, "BID", D("100"), D("1")),),
            )

    p = pipeline()
    p.strategy = FakeMaker()
    with pytest.raises(ValueError, match="cannot simulate maker quotes"):
        CausalBarBacktester(pipeline=p, config=BacktestConfig()).run(bars())


def test_no_fill_when_horizon_shorter_than_next_eligible_open():
    # Horizon 15s is longer than 1s latency but shorter than next tradable
    # minute opening. Requests expire instead of being backdated.
    result = run(bars(), horizon_ms=15_000)
    assert result.fills == ()


def test_rejects_mixed_symbol_out_of_order_and_late_publication():
    good = bars()[:6]
    with pytest.raises(ValueError, match="strictly increasing"):
        run(good[:2] + (good[1],))
    with pytest.raises(ValueError, match="mixed symbols"):
        run(good[:2] + (replace(good[2], symbol="MSFT"),))
    late = good[:2] + (
        replace(good[2], available_at_ms=300_000),
        good[3],
    )
    with pytest.raises(ValueError, match="publication timestamps"):
        run(late)


def test_backtester_rejects_reuse_or_empty_bars():
    simulator = CausalBarBacktester(pipeline=pipeline(), config=BacktestConfig())
    with pytest.raises(ValueError, match="at least one"):
        simulator.run([])
    with pytest.raises(RuntimeError, match="single-use"):
        simulator.run(bars())


@pytest.mark.parametrize("wrong", [
    dict(initial_cash=D("0")),
    dict(initial_cash=D("NaN")),
    dict(latency_ms=True),
    dict(latency_ms=-1),
    dict(order_ttl_ms=0),
    dict(fee_bps=D("-1")),
    dict(slippage_bps=D("10000")),
    dict(quantity_step=D("0")),
    dict(quantity_step=0.1),
    dict(max_abs_target_weight=D("0")),
    dict(rebalance_threshold=D("-0.1")),
])
def test_simulation_config_invalid_values_fail_closed(wrong):
    with pytest.raises(ValueError):
        BacktestConfig(**wrong)


def test_strategy_engine_module_independent_of_network_and_sdk():
    from pathlib import Path
    import backtest
    assert not hasattr(backtest, "open_trade")
    src = Path(backtest.__file__).with_name("engine.py").read_text("utf-8")
    for prohibited in (
        "from exchanges", "import alpharena", "alpaca.data",
        "from execution", "from market_handler",
    ):
        assert prohibited not in src


def test_average_cost_realized_pnl_and_short_cover_when_position_flips():
    sim = CausalBarBacktester(pipeline=pipeline(), config=BacktestConfig())
    assert sim._apply_position(D("2"), D("100")) == 0
    assert sim._apply_position(D("2"), D("110")) == 0
    assert sim._quantity == D("4") and sim._average_entry == D("105")
    assert sim._apply_position(D("-1"), D("115")) == D("10")
    assert sim._quantity == D("3") and sim._average_entry == D("105")
    assert sim._apply_position(D("-5"), D("120")) == D("45")
    assert sim._quantity == D("-2") and sim._average_entry == D("120")
    assert sim._apply_position(D("2"), D("115")) == D("10")
    assert sim._quantity == 0 and sim._average_entry == 0
    assert sim._realized_price_pnl == D("65")


def test_net_pnl_reconciles_realized_unrealized_and_fees():
    for strategy in ("momentum", "reversion"):
        r = run(bars()[:7], side=strategy)
        m = r.metrics
        assert (
            m.net_pnl ==
            m.realized_price_pnl + m.unrealized_price_pnl - m.total_fees
        )
        assert m.total_slippage_cost >= 0
        assert m.net_return == m.net_pnl / m.initial_equity


def test_later_publication_cannot_retroactively_cancel_earlier_open_fill():
    # Timeline:
    # bar4 publication = 241_000; long intent becomes ready = 242_000.
    # bar6 open        = 300_000; long intent must FILL.
    # bar5 publication = 301_000; later short intent may only now replace it.
    # A naive per-completed-bar loop incorrectly processes the short signal
    # BEFORE the chronologically earlier bar6 open and suppresses the long.
    class FlipAfterOpening(Strategy):
        strategy_id = "momentum"

        def decide(self, ctx):
            observed = ctx.features.observed_at_ms
            if observed not in (4 * MINUTE, 5 * MINUTE):
                return StrategyDecision(self.strategy_id, ctx.decision_at_ms)
            signal = ctx.signals[0]
            weight = D("0.5") if observed == 4 * MINUTE else D("-0.5")
            exposure = TargetExposure(
                decision_id=f"flip:{observed}", symbol=ctx.symbol,
                as_of_ms=ctx.decision_at_ms,
                horizon_ms=signal.horizon_ms, target_weight=weight,
                expected_return=signal.expected_return,
                confidence=signal.confidence, strategy=self.strategy_id,
                signal_ids=(signal.signal_id,),
            )
            return StrategyDecision(
                self.strategy_id, ctx.decision_at_ms, exposures=(exposure,),
            )

    p = pipeline()
    p.strategy = FlipAfterOpening()
    result = CausalBarBacktester(
        pipeline=p,
        config=BacktestConfig(
            latency_ms=1000, fee_bps=D("0"), slippage_bps=D("0"),
        ),
    ).run(bars()[:8])
    assert result.fills
    first = result.fills[0]
    assert first.decision_id == f"flip:{4 * MINUTE}"
    assert first.filled_at_ms == 5 * MINUTE
    assert first.signed_quantity > 0
    assert first.filled_at_ms < bars()[4].available_at_ms
    assert any(
        fill.signed_quantity < 0 and fill.decision_id == f"flip:{5 * MINUTE}"
        for fill in result.fills[1:]
    )


def test_strategy_receives_current_portfolio_without_replay_specific_contract():
    recorded = []

    class PortfolioAwareMomentum(MomentumStrategy):
        def decide(self, context):
            assert context.portfolio is not None
            assert context.portfolio.as_of_ms == context.decision_at_ms
            assert context.portfolio.equity > 0
            recorded.append((
                context.decision_at_ms,
                context.portfolio.positions[context.symbol],
                context.portfolio.equity,
            ))
            return super().decide(context)

    p = pipeline()
    p.strategy = PortfolioAwareMomentum(target_weight=D("0.5"))
    r = CausalBarBacktester(
        pipeline=p, config=BacktestConfig(latency_ms=1000),
    ).run(bars()[:7])
    assert r.fills
    # bar4 publication creates target; bar6 open occurs BEFORE bar5
    # publication and fills it, so the bar5 strategy sees holdings.
    assert recorded[0][0] == 4 * MINUTE + 1000
    assert recorded[0][1] == 0
    assert recorded[1][0] == 5 * MINUTE + 1000
    assert recorded[1][1] > 0
    assert recorded[1][2] == r.equity_curve[4].equity
