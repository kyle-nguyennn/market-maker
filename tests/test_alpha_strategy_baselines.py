"""Deterministic baseline signal models and parallel Strategy-family evaluations.

All tests use synthetic snapshots/bars; there is no Alpaca API, AlphArena trade,
network, account mutation, training job or claim of predictive value.
"""
from dataclasses import replace
from decimal import Decimal as D, localcontext

import pytest

from alpha import (
    AlphaPipeline, AlphaSignal, FeatureConfig, FeatureEngine, FeatureSnapshot,
    MeanReversionStrategy, MomentumStrategy, PortfolioSnapshot, PriceBar,
    QuoteProposal, SignalEngine, Strategy, StrategyContext, StrategyDecision,
    TargetExposure, replay_pipeline,
)
from alpha.signal_models import MeanReversionSignalModel, MomentumSignalModel
from alpha.strategy_engine import StrategyBatch, StrategyEngine
from model.market import BookLevel, MarketState
from model.order import Quote, Side
from quoter.alpha_strategy_adapter import MarketMakingStrategy
from quoter.base_quoter import BaseMMQuoter


def snapshot(**changes):
    fields = dict(
        snapshot_id="snapshot-AAPL-asof-100", symbol="AAPL",
        observed_at_ms=100, available_at_ms=110, lookback_ms=60_000,
        source="mock-price-series-v1", feature_version="price-v1",
        values={
            "momentum": D("0.02"),
            "price_zscore": D("1.5"),
            "realized_vol": D("0.01"),
        },
    )
    fields.update(changes)
    return FeatureSnapshot(**fields)


def models():
    return (
        MomentumSignalModel(
            feature_version="price-v1", model_version="mom-fixed-v1",
            horizon_ms=30_000, forecast_multiplier=D("0.25"),
            min_abs_momentum=D("0.003"), model_score=D("0.6"),
        ),
        MeanReversionSignalModel(
            feature_version="price-v1", model_version="rev-fixed-v1",
            horizon_ms=30_000, forecast_multiplier=D("0.8"),
            min_abs_zscore=D("1"), model_score=D("0.55"),
        ),
    )


def signals(features=None, *, at_ms=120):
    return SignalEngine(models()).on_features(
        snapshot() if features is None else features,
        generated_at_ms=at_ms,
    )


def market(*, at_ms=105, valid=True):
    return MarketState(
        instrument="AAPL", timestamp_ms=at_ms,
        bids=(BookLevel(D("100"), D("3")),),
        asks=(BookLevel(D("101"), D("3")),),
        is_valid=valid,
    )


def context(*, features=None, predictions=None, at_ms=120, market_view=None,
            portfolio=None):
    f = snapshot() if features is None else features
    return StrategyContext(
        symbol=f.symbol, decision_at_ms=at_ms, features=f,
        signals=signals(f, at_ms=at_ms) if predictions is None else predictions,
        market=market_view, portfolio=portfolio,
    )


def test_explicit_coefficients_and_opposite_forecast_hypotheses():
    f = snapshot()
    result = signals(f)
    assert len(result) == 2
    momentum, reversion = result
    assert momentum.strategy == "momentum"
    assert reversion.strategy == "mean_reversion"
    assert momentum.expected_return == D("0.005")
    assert reversion.expected_return == D("-0.012")
    assert momentum.horizon_ms == reversion.horizon_ms == 30_000
    assert momentum.confidence == D("0.6")
    assert reversion.confidence == D("0.55")
    assert momentum.snapshot_id == reversion.snapshot_id == f.snapshot_id
    assert all(s.feature_available_at_ms == f.available_at_ms for s in result)


def test_signal_ids_are_reproducible_and_parameter_sensitive():
    f = snapshot()
    models_v1 = models()
    first = SignalEngine(models_v1).on_features(f, generated_at_ms=120)
    second = SignalEngine(models_v1).on_features(f, generated_at_ms=120)
    assert first == second
    assert first[0].signal_id != first[1].signal_id
    assert first != SignalEngine(models_v1).on_features(f, generated_at_ms=121)
    changed = replace(models_v1[0], forecast_multiplier=D("0.3"))
    assert changed.predict(f, generated_at_ms=120).signal_id != first[0].signal_id
    assert models_v1[0].predict(
        snapshot(snapshot_id="another-asof"), generated_at_ms=120,
    ).signal_id != first[0].signal_id


def test_fixed_precision_does_not_depend_on_caller_decimal_context():
    f = snapshot(values={
        "momentum": D("0.012345678901234567890123456789"),
        "realized_vol": D("0.0012345678901234567890123456789"),
        "price_zscore": D("1.9876543210987654321"),
    })
    first = signals(f)
    with localcontext() as dc:
        dc.prec = 6
        dc.rounding = "ROUND_DOWN"
        second = signals(f)
    assert first == second


@pytest.mark.parametrize("invalid", [
    dict(forecast_multiplier=D("0")),
    dict(forecast_multiplier=D("-1")),
    dict(forecast_multiplier=0.2),
    dict(forecast_multiplier=D("Infinity")),
    dict(horizon_ms=0),
    dict(horizon_ms=True),
    dict(feature_version=""),
    dict(model_version=" "),
    dict(min_abs_momentum=D("-1")),
    dict(model_score=D("1.01")),
    dict(model_score=D("NaN")),
])
def test_momentum_model_rejects_invalid_config(invalid):
    kwargs = dict(
        feature_version="price-v1", model_version="v1",
        horizon_ms=30000, forecast_multiplier=D("0.25"),
    )
    kwargs.update(invalid)
    with pytest.raises(ValueError):
        MomentumSignalModel(**kwargs)


@pytest.mark.parametrize("invalid", [
    dict(forecast_multiplier=D("0")),
    dict(forecast_multiplier=D("-0.1")),
    dict(min_abs_zscore=D("-0.1")),
    dict(horizon_ms=0),
    dict(model_score=0.5),
])
def test_reversion_model_rejects_invalid_config(invalid):
    kwargs = dict(
        feature_version="price-v1", model_version="v1",
        horizon_ms=30000, forecast_multiplier=D("0.8"),
    )
    kwargs.update(invalid)
    with pytest.raises(ValueError):
        MeanReversionSignalModel(**kwargs)


def test_models_abstain_on_weak_flat_or_nonvolatile_data():
    weak = snapshot(values={
        "momentum": D("0.001"), "price_zscore": D("0.1"),
        "realized_vol": D("0.01"),
    })
    assert SignalEngine(models()).on_features(weak, generated_at_ms=120) == ()
    flat = snapshot(values={
        "momentum": D("0"), "price_zscore": D("0"),
        "realized_vol": D("0"),
    })
    assert SignalEngine(models()).on_features(flat, generated_at_ms=120) == ()
    zero_vol = snapshot(values={
        "momentum": D("0.01"), "price_zscore": D("2"),
        "realized_vol": D("0"),
    })
    only_momentum = SignalEngine(models()).on_features(zero_vol, generated_at_ms=120)
    assert len(only_momentum) == 1
    assert only_momentum[0].strategy == "momentum"


def test_models_reject_missing_features_versions_and_future_timestamps():
    mom, rev = models()
    with pytest.raises(ValueError, match="version mismatch"):
        mom.predict(snapshot(feature_version="price-v2"), generated_at_ms=120)
    with pytest.raises(ValueError, match="version mismatch"):
        rev.predict(snapshot(feature_version="price-v2"), generated_at_ms=120)
    with pytest.raises(ValueError, match="missing"):
        mom.predict(snapshot(values={"realized_vol": D("0.1")}), generated_at_ms=120)
    with pytest.raises(ValueError, match="needs zscore"):
        rev.predict(snapshot(values={"momentum": D("0.1")}), generated_at_ms=120)
    with pytest.raises(ValueError, match="future feature"):
        mom.predict(snapshot(), generated_at_ms=109)
    with pytest.raises(ValueError, match="future feature"):
        rev.predict(snapshot(), generated_at_ms=109)


def test_negative_momentum_and_negative_zscore_reverse_forecast_sides():
    f = snapshot(values={
        "momentum": D("-0.02"), "price_zscore": D("-1.5"),
        "realized_vol": D("0.01"),
    })
    m, r = SignalEngine(models()).on_features(f, generated_at_ms=120)
    assert m.expected_return == D("-0.005")
    assert r.expected_return == D("0.012")
    c = context(features=f, predictions=(m, r))
    assert MomentumStrategy(target_weight=D("0.15")).decide(
        c
    ).exposures[0].target_weight == D("-0.15")
    assert MeanReversionStrategy(target_weight=D("0.12")).decide(
        c
    ).exposures[0].target_weight == D("0.12")


class MockMMQuoter(BaseMMQuoter):
    def fair_price(self, state):
        return state.mid

    def reservation_price(self, state, fair):
        return fair

    def calculate_spread(self, state, fair, reservation):
        return D("0.5"), D("0.5")

    def calculate_size(self, state, bid_price, ask_price):
        return D("1"), D("2")


def strategies():
    return (
        MomentumStrategy(target_weight=D("0.1"), min_edge=D("0.001")),
        MeanReversionStrategy(target_weight=D("0.12"), min_edge=D("0.001")),
        MarketMakingStrategy(MockMMQuoter(), strategy_id="maker"),
    )


def test_strategy_engine_evaluates_mm_and_directional_independently():
    registry = StrategyEngine(strategies())
    ctx = context(market_view=market(), portfolio=PortfolioSnapshot(
        as_of_ms=119, equity=D("10000"), positions={"AAPL": D("5")},
    ))
    batch = registry.evaluate(ctx)
    assert isinstance(batch, StrategyBatch)
    assert batch.symbol == "AAPL"
    assert batch.snapshot_id == ctx.features.snapshot_id
    assert registry.strategy_ids == ("momentum", "mean_reversion", "maker")
    assert [d.strategy_id for d in batch.decisions] == list(registry.strategy_ids)
    assert batch.skipped_reason is None
    assert batch.decisions[0].exposures[0].target_weight == D("0.1")
    assert batch.decisions[1].exposures[0].target_weight == D("-0.12")
    assert batch.decisions[2].quotes == (
        QuoteProposal("AAPL", "BID", D("100"), D("1")),
        QuoteProposal("AAPL", "ASK", D("101"), D("2")),
    )
    assert all(x.quotes == () for x in batch.decisions[:2])
    assert batch.decisions[2].exposures == ()


def test_strategy_engine_accepts_missing_market_for_directional_but_mm_abstains():
    batch = StrategyEngine(strategies()).evaluate(context())
    assert batch.decisions[0].exposures
    assert batch.decisions[1].exposures
    assert batch.decisions[2].quotes == ()
    assert batch.skipped_reason is None


@pytest.mark.parametrize("reason,limits,kwargs", [
    ("invalid_market", {}, dict(market_view=market(valid=False))),
    ("stale_features", dict(max_feature_age_ms=5), {}),
    ("stale_market", dict(max_market_age_ms=5),
     dict(market_view=market(at_ms=105))),
    ("stale_portfolio", dict(max_portfolio_age_ms=5),
     dict(portfolio=PortfolioSnapshot(
         as_of_ms=100, equity=D("1000"), positions={},
     ))),
])
def test_strategy_engine_abstains_whole_batch_on_quality_gates(reason, limits, kwargs):
    engine = StrategyEngine(strategies(), **limits)
    batch = engine.evaluate(context(**kwargs))
    assert batch.skipped_reason == reason
    assert len(batch.decisions) == 3
    assert all(d.exposures == () and d.quotes == () for d in batch.decisions)


def test_strategy_engine_threshold_is_inclusive_at_boundary():
    registry = StrategyEngine(
        [MomentumStrategy(target_weight=D("0.1"))],
        max_feature_age_ms=10,
    )
    c = context(at_ms=120)
    assert registry.evaluate(c).skipped_reason is None
    assert registry.evaluate(context(at_ms=121)).skipped_reason == "stale_features"


def test_strategy_engine_rejects_duplicate_policy_ids_invalid_age_and_empty():
    with pytest.raises(ValueError, match="at least one"):
        StrategyEngine([])
    with pytest.raises(ValueError, match="duplicate strategy_id"):
        StrategyEngine([
            MomentumStrategy(target_weight=D("0.1")),
            MomentumStrategy(target_weight=D("0.2")),
        ])
    with pytest.raises(TypeError, match="Strategy ABC"):
        StrategyEngine([object()])
    for age in [-1, True, 1.5]:
        with pytest.raises(ValueError, match="max_feature_age_ms"):
            StrategyEngine(strategies(), max_feature_age_ms=age)


def test_strategy_engine_fails_closed_on_wrong_time_symbol_signal_lineage():
    f = snapshot()
    c = context(features=f, predictions=signals(f))

    class MalformedPolicy(Strategy):
        strategy_id = "broken"
        def decide(self, context):
            sig = context.signals[0]
            exposure = TargetExposure(
                decision_id="oops", symbol="MSFT",
                as_of_ms=context.decision_at_ms, horizon_ms=sig.horizon_ms,
                target_weight=D("0.1"), expected_return=sig.expected_return,
                confidence=sig.confidence, strategy=self.strategy_id,
                signal_ids=(sig.signal_id,),
            )
            return StrategyDecision(
                strategy_id=self.strategy_id,
                decision_at_ms=context.decision_at_ms,
                exposures=(exposure,),
            )

    with pytest.raises(ValueError, match="symbol mismatch"):
        StrategyEngine([MalformedPolicy()]).evaluate(c)

    class ForgedSignal(MalformedPolicy):
        def decide(self, context):
            sig = context.signals[0]
            return StrategyDecision(
                strategy_id=self.strategy_id,
                decision_at_ms=context.decision_at_ms,
                exposures=(TargetExposure(
                    decision_id="oops2", symbol=context.symbol,
                    as_of_ms=context.decision_at_ms, horizon_ms=sig.horizon_ms,
                    target_weight=D("0.1"), expected_return=sig.expected_return,
                    confidence=sig.confidence, strategy=self.strategy_id,
                    signal_ids=("unseen-signal",),
                ),),
            )
    with pytest.raises(ValueError, match="unknown signal"):
        StrategyEngine([ForgedSignal()]).evaluate(c)

    class WrongDecisionId(MalformedPolicy):
        def decide(self, context):
            return StrategyDecision("another-strategy", context.decision_at_ms)
    with pytest.raises(ValueError, match="ID or time mismatch"):
        StrategyEngine([WrongDecisionId()]).evaluate(c)


def test_strategy_batch_cannot_hide_active_quotes_under_skip():
    with pytest.raises(ValueError, match="skipped batch"):
        StrategyBatch(
            symbol="AAPL", snapshot_id="snap", decision_at_ms=120,
            decisions=(StrategyDecision("maker", 120, quotes=(
                QuoteProposal("AAPL", "BID", D("100"), D("1")),
            )),),
            skipped_reason="stale_market",
        )


def bars():
    result = []
    for i, close in enumerate(["100", "101", "102", "103", "104"], start=1):
        price = D(close)
        result.append(PriceBar(
            symbol="AAPL", source="synthetic-unadjusted-v1",
            end_ms=i * 1000, available_at_ms=i * 1000 + 20,
            interval_ms=1000, open=price, high=price, low=price,
            close=price, volume=D("10"),
        ))
    return result


def pipeline(strategy):
    return AlphaPipeline(
        features=FeatureEngine(
            symbol="AAPL",
            config=FeatureConfig(
                interval_ms=1000, lookback_bars=3,
                max_staleness_ms=2000, feature_version="price-v1",
            ),
        ),
        signals=SignalEngine(models()),
        strategy=strategy,
    )


@pytest.mark.parametrize("policy", [
    MomentumStrategy(target_weight=D("0.15")),
    MeanReversionStrategy(target_weight=D("0.10")),
])
def test_full_pricebar_to_signal_to_strategy_replay_matches_live(policy):
    historical = pipeline(policy)
    live = pipeline(policy)
    replay_results = replay_pipeline(bars(), historical)
    live_results = tuple(
        output for bar in bars()
        if (output := live.on_bar(bar, at_ms=bar.available_at_ms))
        is not None
    )
    assert len(replay_results) == 2
    assert replay_results == live_results
    assert all(result.features.source == "synthetic-unadjusted-v1"
               for result in replay_results)
    assert all(len(result.signals) == 2 for result in replay_results)
    assert all(result.decision.exposures for result in replay_results)
    assert all(result.decision.quotes == () for result in replay_results)


def test_strategy_engine_is_research_only_no_execution_imports():
    import alpha
    import inspect
    import alpha.strategy_engine
    import alpha.signal_models
    for module in (alpha.strategy_engine, alpha.signal_models):
        src = inspect.getsource(module)
        assert "from model." not in src
        assert "from quoter" not in src
        assert "from exchanges" not in src
        assert "from market_handler" not in src
        assert "from alpaca" not in src
    assert hasattr(alpha, "SignalEngine")
