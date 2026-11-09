# Strategy layer — fixed-rule research baselines

This is the next layer after [P2 source-neutral feature/signal engines](./README.md).
It stays in vendored `alpha/` for now and depends only on the standard
library. It never imports any venue, SDK, order, quoter or execution module.

## Objects and ownership

```text
Normalized PriceBar -> FeatureEngine -> FeatureSnapshot
                                          |
                          SignalEngine / SignalModel.predict
                                          |
                              AlphaSignal[] (forecasts)
                                          |
                     StrategyContext (features + signals
                           + optional market + portfolio)
                                          |
           StrategyEngine.evaluate (counterfactual fan-out)
                           /        |        \
                    Momentum   MeanReversion  MarketMaking
                           \        |        /
                     StrategyBatch of separately
                       tagged StrategyDecisions
                                          |
              future selector / portfolio composer / risk
              (NOT implemented; no automatic submission)
```

`AlphaPipeline` remains the single-strategy composition. In contrast,
`StrategyEngine` evaluates *multiple policy families independently* on the
same context, including an existing `MarketMakingStrategy` wrapper imported
from **host-side** `quoter/alpha_strategy_adapter.py`. Its output is not
an allocation, combined executable order set or automatic policy selector.

## Deterministic signal hypotheses

`alpha/signal_models.py` provides two stateless, versioned `SignalModel`
implementations:

| Model | Fixed heuristic fractional-return forecast |
| --- | --- |
| `MomentumSignalModel` | `k * features["momentum"]` |
| `MeanReversionSignalModel` | `-k * features["price_zscore"] * features["realized_vol"]` |

`realized_vol` is **per-bar** fractional volatility, not annualized. Both
`k` coefficients are mandatory and positive to preserve the expected
directional interpretation. The chosen `horizon_ms` must be specified
independently of the trailing lookback. These formulas are **heuristic
hypotheses**, not regression estimates, calibrated expected returns or
guarantees. `model_score` is a fixed, uncalibrated field consumed as
`AlphaSignal.confidence`; it is *not* a win probability.

Other controls:

- `min_abs_momentum`/`min_abs_zscore`: abstain when the observed feature
  magnitude fails to exceed the threshold. Mean reversion also abstains
  when `realized_vol == 0`.
- `feature_version`: require the upstream feature semantics to match exactly;
  no silent inference on a different feature schema or revision.
- `model_version`: explicit user-supplied research model identifier.
  Signal ID is SHA-256 over model identity, full parameter configuration,
  feature snapshot ID, generation time, horizon and resulting value.
- Forecast multiplication runs under a fixed 34-digit Decimal context.
  Input features are immutable `Decimal` values, never floats.
- Fail closed for unavailable features, invalid configuration, wrong
  version or future generation timestamps.

A time-invariant `model_score` and fixed threshold must not be optimized
by looking at the test set. Fitted models and confidence calibration
require a time-separated training/validation workflow and independent
holdout.

## Single-strategy experiment

```python
from decimal import Decimal
from alpha import (
    AlphaPipeline, FeatureConfig, FeatureEngine, MomentumSignalModel,
    MomentumStrategy, SignalEngine, replay_pipeline,
)

pipeline = AlphaPipeline(
    features=FeatureEngine(
        symbol="AAPL",
        config=FeatureConfig(
            interval_ms=60_000, lookback_bars=20,
            max_staleness_ms=120_000,
            feature_version="us-equity-prices-v1",
        ),
    ),
    signals=SignalEngine([
        MomentumSignalModel(
            feature_version="us-equity-prices-v1",
            model_version="fixed-mom-hypothesis-v1",
            horizon_ms=5 * 60_000,
            forecast_multiplier=Decimal("0.25"),
            min_abs_momentum=Decimal("0.003"),
            model_score=Decimal("0.5"),
        ),
    ]),
    strategy=MomentumStrategy(
        target_weight=Decimal("0.05"),
        min_edge=Decimal("0.001"),
    ),
)

# Load normalized completed OHLCV observations from the HOST data adapter.
# Historical and live feed into the same pipeline.on_bar method.
results = replay_pipeline(bars, pipeline)
```

The numerical parameters in this example are **illustrative**, not
optimized. `min_edge` is a forecast threshold, not actual measured
transaction costs; it should ultimately be compared to realistic
fees, slippage and turnover. An empty decision means abstention, not
automatic liquidation of an existing position.

## Multi-family research comparison

```python
from decimal import Decimal
from alpha import (
    StrategyContext, StrategyEngine, MomentumStrategy,
    MeanReversionStrategy,
)

engine = StrategyEngine([
    MomentumStrategy(target_weight=Decimal("0.05")),
    MeanReversionStrategy(target_weight=Decimal("0.05")),
    # host can also register MarketMakingStrategy(BaseMMQuoter(...))
], max_feature_age_ms=120_000)

context = StrategyContext(
    symbol=features.symbol,
    decision_at_ms=decision_at_ms,
    features=features,
    signals=signals,
    market=market_state,       # optional venue-neutral view
    portfolio=portfolio_view,  # optional immutable view
)
batch = engine.evaluate(context)
for decision in batch.decisions:
    # Store or evaluate counterfactual proposals; DO NOT send directly.
    print(decision.strategy_id, decision.exposures, decision.quotes)
```

The engine validates strategy IDs, timestamps, target symbols,
`signal_ids` lineage and quote market availability. An explicitly
invalid market or an exceeded caller-configured feature/market/portfolio
age limit causes a *whole-batch abstention* with a `skipped_reason`.
Age gates are optional and **not** production risk authorization.

## Next research milestones

1. **Session-aware normalized equities bars**: the existing
   `FeatureEngine` requires contiguous UTC intervals and drops its
   window across market closes. Real Alpaca equity data also has no-trade
   minute gaps, revised bars and corporate-action updates. No serious
   leakage-free AlphArena study until these cases have explicit policies.
2. **Walk-forward baseline evaluation**: compare fixed momentum,
   fixed mean reversion and an abstention/no-trade policy using identical
   as-of snapshots, target horizons and holdout periods. Include actual
   transaction-cost/turnover assumptions and benchmark against naive
   fixed allocations.
3. **Strategy selector**: only after the baselines are measured,
   introduce fixed transparent routing and then regime-aware/adaptive
   selection with no peeking at holdout outcomes. The current
   `StrategyEngine` deliberately does not select a winner.
4. **Portfolio composition/risk**: translate approved target exposures
   or quote proposals into constrained actions through the appropriate
   AlphArena/SHIFT venue adapters. This is **not implemented** and
   requires separate authorization and account reconciliation.
5. **MM comparison**: maker quoting cannot be compared to
   position-based AlphArena on paper returns alone. Use order-book
   replay/SHIFT to measure fills, queue behavior, rebates and adverse
   selection; keep MM quote decisions as a separate policy family.

Tests: `python -m pytest -q tests/test_alpha_strategy_baselines.py`.
All data, models and actions in tests are synthetic; no network calls.
