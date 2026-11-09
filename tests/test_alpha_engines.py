"""P2: shared live/replay API, isolation, signal provenance, strategy families."""
from dataclasses import FrozenInstanceError
from decimal import Decimal as D

import pytest

from alpha import (
    AlphaPipeline, AlphaSignal, FeatureConfig, FeatureEngine,
    MeanReversionStrategy, MomentumStrategy, PortfolioSnapshot,
    PriceBar, PriceFeatureCalculator, QuoteProposal, SignalEngine,
    Strategy, StrategyContext, StrategyDecision, replay_bars, replay_pipeline,
)
from model.market import BookLevel, MarketState
from model.order import Quote, Side
from quoter.alpha_strategy_adapter import MarketMakingStrategy
from quoter.base_quoter import BaseMMQuoter


def config():
    return FeatureConfig(
        interval_ms=1000, lookback_bars=3, max_staleness_ms=1500,
        feature_version="price-v1",
    )


def bar(i, price=None, **overrides):
    p = D(i + 99 if price is None else price)
    values = dict(
        symbol="AAPL", source="versioned-bars-v1",
        end_ms=i * 1000, available_at_ms=i * 1000 + 20,
        interval_ms=1000, open=p, high=p, low=p,
        close=p, volume=D("10"),
    )
    values.update(overrides)
    return PriceBar(**values)


class MomentumModel:
    strategy_id = "momentum"

    def predict(self, features, *, generated_at_ms):
        return AlphaSignal(
            signal_id=f"mom:{features.snapshot_id}",
            snapshot_id=features.snapshot_id,
            symbol=features.symbol,
            strategy=self.strategy_id, model_version="test-v1",
            generated_at_ms=generated_at_ms,
            feature_available_at_ms=features.available_at_ms,
            horizon_ms=30_000,
            expected_return=features.values["momentum"],
            confidence=D("0.8"),
            regime="TREND",
        )


class ReversionModel:
    strategy_id = "mean_reversion"

    def predict(self, features, *, generated_at_ms):
        return AlphaSignal(
            signal_id=f"rev:{features.snapshot_id}",
            snapshot_id=features.snapshot_id,
            symbol=features.symbol,
            strategy=self.strategy_id, model_version="test-v1",
            generated_at_ms=generated_at_ms,
            feature_available_at_ms=features.available_at_ms,
            horizon_ms=30_000,
            expected_return=-features.values["momentum"],
            confidence=D("0.7"),
        )


def pipeline(strategy=None, models=None):
    return AlphaPipeline(
        features=FeatureEngine(symbol="AAPL", config=config()),
        signals=SignalEngine([MomentumModel()] if models is None else models),
        strategy=MomentumStrategy(target_weight=D("0.15")) if strategy is None else strategy,
    )


def bars():
    return [bar(i) for i in range(1, 7)]


def market(**overrides):
    values = dict(
        instrument="AAPL", timestamp_ms=4000,
        bids=(BookLevel(D("102"), D("3")),),
        asks=(BookLevel(D("104"), D("3")),),
    )
    values.update(overrides)
    return MarketState(**values)


def test_replay_and_live_feed_use_exact_same_on_bar_and_policy():
    historical = pipeline()
    live = pipeline()
    from_replay = replay_pipeline(bars(), historical)
    from_live = tuple(
        result for observation in bars()
        if (result := live.on_bar(
            observation, at_ms=observation.available_at_ms,
        )) is not None
    )
    assert from_replay == from_live
    assert len(from_replay) == 3  # warmup needs lookback+1 bars
    assert all(result.decision.strategy_id == "momentum" for result in from_replay)
    assert all(result.decision.exposures[0].target_weight == D("0.15")
               for result in from_replay)
    assert all(result.decision.quotes == () for result in from_replay)


def test_replay_order_determined_by_publication_not_input_order():
    engine = FeatureEngine(symbol="AAPL", config=config())
    first = replay_bars(list(reversed(bars())), engine)
    assert [s.observed_at_ms for s in first] == [4000, 5000, 6000]
    assert engine.bars_buffered == 4
    assert engine.gaps_seen == 0


def test_pure_calculator_and_engine_produce_same_observations():
    from alpha import build_price_features
    history = bars()[:4]
    expected = build_price_features(
        history, symbol="AAPL", config=config(),
        decision_at_ms=4020,
    )
    engine = FeatureEngine(
        symbol="AAPL", config=config(), calculator=PriceFeatureCalculator(),
    )
    snapshots = replay_bars(history, engine)
    assert snapshots == (expected,)
    assert engine.snapshot(at_ms=4020) == expected
    delayed = engine.snapshot(at_ms=4030)
    assert delayed.snapshot_id != expected.snapshot_id
    assert delayed.values == expected.values


def test_feature_engine_rewarms_after_gap_and_rejects_cross_symbol_and_revision():
    engine = FeatureEngine(symbol="AAPL", config=config())
    assert replay_bars(bars()[:4], engine)
    assert engine.bars_buffered == 4
    assert engine.on_bar(bar(6), at_ms=6020) is None
    assert engine.gaps_seen == 1 and engine.bars_buffered == 1
    assert replay_bars([bar(7), bar(8), bar(9)], engine)[-1].observed_at_ms == 9000
    with pytest.raises(ValueError, match="duplicate|out-of-order"):
        engine.on_bar(bar(9), at_ms=9020)
    with pytest.raises(ValueError, match="cross-symbol"):
        engine.on_bar(bar(10, symbol="MSFT"), at_ms=10020)
    with pytest.raises(ValueError, match="source/version"):
        engine.on_bar(bar(10, source="new-vendor-v2"), at_ms=10020)
    assert engine.bars_buffered == 4


def test_feature_engine_rejects_future_and_backwards_clock():
    engine = FeatureEngine(symbol="AAPL", config=config())
    with pytest.raises(ValueError, match="not yet available"):
        engine.on_bar(bar(1), at_ms=1000)
    engine.on_bar(bar(1), at_ms=1020)
    with pytest.raises(ValueError, match="clock moved backwards"):
        engine.on_bar(bar(2, available_at_ms=2000), at_ms=1000)
    with pytest.raises(ValueError, match="before last ingested"):
        engine.snapshot(at_ms=1019)
    engine.reset()
    assert engine.bars_buffered == 0
    assert engine.on_bar(bar(1), at_ms=1020) is None


def test_late_publication_out_of_order_does_not_rewrite_history():
    engine = FeatureEngine(symbol="AAPL", config=config())
    engine.on_bar(bar(1), at_ms=1020)
    engine.on_bar(bar(3), at_ms=3020)  # explicit gap -> warmup reset
    with pytest.raises(ValueError, match="out-of-order"):
        engine.on_bar(bar(2, available_at_ms=3500), at_ms=3500)
    assert engine.gaps_seen == 1 and engine.bars_buffered == 1


def test_signal_engine_provenance_and_independent_signal_families():
    from alpha import build_price_features
    features = build_price_features(
        bars()[:4], symbol="AAPL", decision_at_ms=4020, config=config(),
    )
    engine = SignalEngine([MomentumModel(), ReversionModel()])
    signals = engine.on_features(features, generated_at_ms=4020)
    assert [s.strategy for s in signals] == ["momentum", "mean_reversion"]
    assert engine.latest("AAPL") == signals
    assert signals[0].expected_return > 0
    assert signals[1].expected_return < 0
    ctx = StrategyContext(
        symbol="AAPL", decision_at_ms=4020,
        features=features, signals=signals,
    )
    assert MomentumStrategy(target_weight=D("0.15")).decide(ctx).exposures[0].target_weight == D("0.15")
    assert MeanReversionStrategy(target_weight=D("0.10")).decide(ctx).exposures[0].target_weight == D("-0.10")
    assert isinstance(MomentumStrategy(target_weight=D("0.15")), Strategy)


def test_signal_engine_rejects_future_or_inconsistent_model():
    from alpha import build_price_features
    features = build_price_features(
        bars()[:4], symbol="AAPL", decision_at_ms=4020, config=config(),
    )
    engine = SignalEngine([MomentumModel()])
    with pytest.raises(ValueError, match="not yet available"):
        engine.on_features(features, generated_at_ms=4019)

    class BrokenModel(MomentumModel):
        def predict(self, features, *, generated_at_ms):
            from dataclasses import replace
            return replace(super().predict(
                features, generated_at_ms=generated_at_ms,
            ), snapshot_id="fake-historical-snapshot")

    with pytest.raises(ValueError, match="provenance"):
        SignalEngine([BrokenModel()]).on_features(
            features, generated_at_ms=4020,
        )
    with pytest.raises(ValueError, match="duplicate signal model"):
        SignalEngine([MomentumModel(), MomentumModel()])


def test_portfolio_context_immutable_and_future_inputs_rejected():
    from alpha import build_price_features
    features = build_price_features(
        bars()[:4], symbol="AAPL", decision_at_ms=4020, config=config(),
    )
    raw = {"AAPL": D("5")}
    portfolio = PortfolioSnapshot(as_of_ms=4000, equity=D("10000"), positions=raw)
    raw["AAPL"] = D("9000")
    assert portfolio.positions["AAPL"] == 5
    with pytest.raises(TypeError):
        portfolio.positions["AAPL"] = D("2")
    with pytest.raises(FrozenInstanceError):
        portfolio.equity = D("5")
    ctx = StrategyContext(
        symbol="AAPL", decision_at_ms=4020, features=features,
        signals=(), market=market(), portfolio=portfolio,
    )
    assert ctx.market is not None and ctx.portfolio is not None
    with pytest.raises(ValueError, match="future feature"):
        StrategyContext(symbol="AAPL", decision_at_ms=4019,
                        features=features, signals=())
    with pytest.raises(ValueError, match="future portfolio"):
        StrategyContext(symbol="AAPL", decision_at_ms=4020,
                        features=features, signals=(),
                        portfolio=PortfolioSnapshot(
                            as_of_ms=5000, equity=D("10000"), positions={},
                        ))
    with pytest.raises(ValueError, match="future market"):
        StrategyContext(symbol="AAPL", decision_at_ms=4020,
                        features=features, signals=(),
                        market=market(timestamp_ms=5000))


class TestQuoter(BaseMMQuoter):
    def fair_price(self, state):
        return state.mid

    def reservation_price(self, state, fair):
        return fair

    def calculate_spread(self, state, fair, reservation):
        return D("1"), D("1")

    def calculate_size(self, state, bid_price, ask_price):
        return D("2"), D("3")


def test_existing_mmquoter_is_a_unified_strategy_family():
    from alpha import build_price_features
    features = build_price_features(
        bars()[:4], symbol="AAPL", decision_at_ms=4020, config=config(),
    )
    ctx = StrategyContext(
        symbol="AAPL", decision_at_ms=4020, features=features,
        signals=(), market=market(),
    )
    adapter = MarketMakingStrategy(TestQuoter(), strategy_id="maker-v1")
    assert isinstance(adapter, Strategy)
    decision = adapter.decide(ctx)
    assert decision.exposures == ()
    assert [(q.side, q.price, q.size) for q in decision.quotes] == [
        ("BID", D("102"), D("2")), ("ASK", D("104"), D("3")),
    ]
    bad = StrategyContext(
        symbol="AAPL", decision_at_ms=4020, features=features, signals=(),
        market=market(is_valid=False),
    )
    assert adapter.decide(bad).quotes == ()


def test_contextual_market_maker_can_read_signals_and_features():
    from alpha import build_price_features
    features = build_price_features(
        bars()[:4], symbol="AAPL", decision_at_ms=4020, config=config(),
    )
    signals = SignalEngine([MomentumModel()]).on_features(features, generated_at_ms=4020)
    ctx = StrategyContext(
        symbol="AAPL", decision_at_ms=4020,
        features=features, signals=signals, market=market(),
    )

    class ContextualQuoter:
        def quote_context(self, context):
            assert context.features.values["momentum"] > 0
            assert context.signals[0].strategy == "momentum"
            assert context.market is not None
            return [Quote(price=D("102"), qty=D("2"), side=Side.BID)]

    adapter = MarketMakingStrategy(ContextualQuoter())
    assert adapter.decide(ctx).quotes == (
        QuoteProposal(symbol="AAPL", side="BID", price=D("102"), size=D("2")),
    )


def test_pipeline_validates_strategy_outputs_not_just_inputs():
    class WrongFamily:
        strategy_id = "wrong-family"
        def decide(self, context):
            return StrategyDecision("impersonated", context.decision_at_ms)

    p = pipeline(strategy=WrongFamily())
    with pytest.raises(ValueError, match="decision id"):
        for one in bars():
            p.on_bar(one, at_ms=one.available_at_ms)


def test_directional_weak_signal_abstains_without_implicit_flattening():
    p = pipeline(strategy=MomentumStrategy(
        target_weight=D("0.15"), min_edge=D("1"),
    ))
    results = replay_pipeline(bars(), p)
    assert len(results) == 3
    assert all(r.decision.exposures == () and r.decision.quotes == ()
               for r in results)


def test_core_alpha_package_has_no_external_venue_or_mmquoter_imports():
    import alpha
    import sys
    assert "quoter" not in alpha.__dict__
    from pathlib import Path
    package = Path(alpha.__file__).parent
    for path in package.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "from quoter" not in text
        assert "from exchanges" not in text
        assert "from execution" not in text
        assert "from model." not in text
