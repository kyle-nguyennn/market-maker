# Alpha — vendored, independently publishable package

This Python package currently lives under `market-maker/alpha`, but is
architected for a future standalone release. It uses only the Python standard
library and other modules within `alpha/`. The existing market-maker-specific
MM quoter bridge is intentionally outside this package in
`quoter/alpha_strategy_adapter.py`.

Read [package constraints](./AGENTS.md) and the durable architecture decisions
under [notes/alpha_engine](../notes/alpha_engine/README.md) before changing a
public contract.

## Composition

```text
             historical replay or live normalized completed-bar feed
                                    |
                          AlphaPipeline.on_bar
                                    |
                         FeatureEngine.on_bar
                         (bounded rolling state)
                                    |
                       PriceFeatureCalculator
                           (pure calculation)
                                    |
                             FeatureSnapshot
                                    |
                       SignalEngine.on_features
                        SignalModel.predict(...)
                                    |
                             AlphaSignal[]
                                    |
        StrategyContext(market?, features, signals, portfolio?)
                                    |
                            Strategy.decide
                             /          \
                   TargetExposure[]  QuoteProposal[]
                                    |
                        external risk / execution
```

`PriceFeatureCalculator` is replaceable via `FeatureCalculator` Protocol.
`FeatureEngine` does not own HTTP requests or a persistent database. The live
hot path must not depend on Redis or block on historical archiving.

## Public interfaces

- `PriceBar` — validated immutable, complete OHLCV observation, with completed
  bar end and first publication/availability time in UTC milliseconds.
- `FeatureConfig` and `FeatureEngine` — typed rolling config and bounded
  source-agnostic state machine. `FeatureEngine.on_bar(bar, at_ms=...)` is the
  **same method** for historical replay and live normalized bars.
- `FeatureSnapshot` — immutable as-of features, source/version, availability,
  SHA-256 lineage. No float-based inputs or accidental future rows.
- `SignalModel.predict(snapshot, generated_at_ms=...)` — clock-injected
  forecast interface. The older `AlphaStrategy.generate_signal(snapshot)`
  remains import-compatible for P1 callers; new models should use SignalModel.
- `SignalEngine.on_features` — calls configured models and validates immutable
  `AlphaSignal` provenance; does not size exposures or send orders.
- `StrategyContext` — symbol, decision time, matching feature/signal snapshots,
  optionally market/portfolio views. Strategies do not know the data source.
- `Strategy.decide(context)` — common policy-family interface returning
  `StrategyDecision`: either desired `TargetExposure` allocations, desired
  `QuoteProposal` quotes, or an empty decision (abstain). Both are non-executable.
- `MomentumStrategy` and `MeanReversionStrategy` — simple fixed-weight
  directional policies consuming forecasts from SignalEngine. They are **not
  trained predictors** and are not validated profitable strategies.
- `AlphaPipeline.on_bar` — optional composition of the three stages, returning
  all immutable intermediate outputs in `PipelineResult`.

## Replay and live API parity

```python
from decimal import Decimal

from alpha import (
    AlphaPipeline, FeatureConfig, FeatureEngine,
    MomentumStrategy, SignalEngine,
)

pipeline = AlphaPipeline(
    features=FeatureEngine(
        symbol="AAPL",
        config=FeatureConfig(
            interval_ms=60_000,
            lookback_bars=20,
            max_staleness_ms=120_000,
            feature_version="momentum-features-v1",
        ),
    ),
    # Register separately implemented and versioned SignalModel predictors.
    signals=SignalEngine(models=[]),
    strategy=MomentumStrategy(target_weight=Decimal("0.05")),
)

# The SAME call is made by a historical replay adapter and a live bar feed.
# Warmup returns None; no registered models means directional strategy abstains.
# Neither code path submits a trade.
result = pipeline.on_bar(normalized_bar, at_ms=observation_arrival_ms)
```

Here `normalized_bar` is a `PriceBar` produced by the external feed.
For pure historical tests, `replay_pipeline(bars, pipeline)` dispatches to
`pipeline.on_bar()` in publication-availability order. A live connector
should construct the same `PriceBar` without leaking venue SDK types.

For the existing market-making quoter, import
`quoter.alpha_strategy_adapter.MarketMakingStrategy` from the **host
repository**, not from this publishable package. It supports both legacy
`BaseMMQuoter.quote(MarketState)` and context-aware
`quote_context(StrategyContext)`, which can consume signals/features while
still returning quotes for the downstream risk gateway.

## Important non-goals and limits

- Strict `interval_ms` contiguity requires a future exchange-session-aware
  normalizer for AlphArena stocks/ETFs; current code refuses weekend gaps.
- Completed OHLCV bars are not sufficient for SHIFT tick/order-book signals.
  Those need new observation/calculator types with the same engine boundaries.
- `SignalEngine` does not contain calibrated momentum/reversion predictors.
  The current policy families are useful for wiring/contract tests only.
- No external feature store/Redis persistence, async writer, ordered L2 event
  handling, fill simulation, portfolio reconciliation or trade authorization.
- New statistical feature/model releases require versioned data contracts,
  train/test provenance, cost-aware walk-forward evaluation and leak checks.
- Library extraction still requires package metadata, semantic versioning,
  typed serialization contracts and independent import/CI tests.
