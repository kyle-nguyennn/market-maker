# Next-agent handoff — shared alpha engine

**Date:** 2026-11-09; **active P2-A branch:** `feat/alpha-p2-point-in-time-features`; **base:** `main` containing merged P1 [PR #15](https://github.com/kyle-nguyennn/market-maker/pull/15).

## First five minutes

1. Read [README](./README.md), [DESIGN_DECISIONS](./DESIGN_DECISIONS.md), [IMPLEMENTATION_LOG](./IMPLEMENTATION_LOG.md) and `exchanges/alpharena/NOTES.md`.
2. Inspect `model/AGENTS.md`, `alpha/domain.py`, `alpha/observations.py`, `market_handler/historical_bar_reader.py`, `alpha/calculators.py`, `alpha/features.py`, `alpha/feature_engine.py`, `alpha/signal_engine.py`, `alpha/strategy.py`, `alpha/strategies.py`, `alpha/pipeline.py`, `quoter/alpha_strategy_adapter.py` and `tests/test_alpha_engines.py`.
3. Get the latest P2-A PR number/head SHA and check GitHub Actions. Test the latest commit; do not reuse earlier successful check results for newer commits.
4. Keep prices/features finite `Decimal`, snapshots immutable, and code free of venue SDKs and submission paths. Preserve P1 exports.
5. Append or revise the implementation log with actual CI links, known failures and next-agent instructions. Add ADRs for changed decisions.

## Current P2-A work

- New immutable OHLCV bar type separates completed-bar end from actual first-publication time.
- Strict CSV ingestion uses user-specified source (dataset revision) and time interval; no guessed timezone/corporate-action adjustment.
- Pure `build_price_features(..., decision_at_ms=..., config=...)` filters *as-of-available* bars and emits a P1 `FeatureSnapshot`.
- Features are one-period simple return, multi-bar momentum, per-bar realized volatility, and close z-score. Fixed 34-digit Decimal context; SHA-256 snapshot identity over consumed historical data and config.
- Validates no duplicate bar ends, no gaps inside the lookback, no stale latest bar, no mixed source/interval. Future data does not influence features or snapshot ID.
- Tests cover near-future observations, late publications, duplicate revisions, strict CSV values, config validation, caller Decimal context and immutable models.

## Updated architecture: reusable vendored library

```text
PriceBar (live or replay) -> FeatureEngine.on_bar
                              |-- bounded rolling window
                              |-- PriceFeatureCalculator (pure)
                              v
                          FeatureSnapshot
                              v
                          SignalEngine / SignalModel.predict
                              v
                          AlphaSignal[]
                              v
                     StrategyContext + MarketView + PortfolioSnapshot
                              v
                        Strategy.decide
                         /           \\
                 TargetExposure[]  QuoteProposal[]
                         |           |
                      risk/venue execution (future)
```

- The entire `alpha/` directory is **vendored-for-now**. Do not import `model/`, `quoter/`, `exchanges/`, or external SDKs into it. Later packaging/extraction can preserve its public interface without migrating it now.
- Market-maker-specific `MarketMakingStrategy` wraps `BaseMMQuoter` at `quoter/alpha_strategy_adapter.py`; context-aware quoters implement `quote_context(context)` to read signals/features. Historical or live origins are deliberately absent from `StrategyContext`.
- New policies implement `Strategy.decide(context)`, not P1's old `AlphaStrategy.generate_signal(snapshot)`. The older protocol stays for compatibility; new predictors implement `SignalModel.predict(snapshot, generated_at_ms=...)`.
- `MomentumStrategy` / `MeanReversionStrategy` currently only turn an **existing forecast** into a fixed desired weight. They are not calibrated alpha models, do not estimate returns themselves, and cannot trade. Explicit input models must be supplied to `SignalEngine`.
- Reference `PriceFeatureCalculator` recomputes from a small bounded OHLCV window for exact reproducibility. Incremental O(1) feature updates and event-level SHIFT calculators belong to later performance/feature work.
- The pipeline returns non-executable decisions. There is no market-data-store reader on the live hot path, no risk bypass, no broker integration, and no live trading authorization.

## Market-data ownership / AlphArena clarification (ADR-014)

- `alpha/observations.py` owns `PriceBar`, `BarProvider` and validation contracts, but **no data-source I/O**. `alpha/market_data.py` was removed.
- The strict historical CSV reader moved to host `market_handler/historical_bar_reader.py`. Future external equity/crypto providers and Parquet readers belong on this host side, returning the **same** normalized `PriceBar`.
- Historical replay and live normalized bar producers call `FeatureEngine.on_bar(bar, at_ms=...)` or `AlphaPipeline.on_bar`; no strategy-visible source distinction.
- `market_handler/hyperliquid_market_data_handler.py` is currently a Hyperliquid L2 order-book adapter, not a general market-price feed for AlphArena equities. Its `MarketState` snapshots do not themselves contain OHLCV.
- The [AlphArena paper trading API](https://alpharena.io/docs) exposes trade/portfolio/performance operations and marks stock fills internally. No documented public OHLCV stream was identified. Do **not** create a hypothetical `AlphArenaMarketDataHandler` for unsupported endpoints; choose an independent equities feed, with explicit bar availability and corporate-action/session normalization.
- `tests/test_market_data_boundary.py` exercises the new module ownership and replay/live parity. Ensure PR #16 latest CI passes after this change.

## Priority follow-ups

1. Validate P2-A latest-head CI, fix test failures, review and merge when approved. CI run #42579976084 passed 338 tests twice on the engine-refactor code commit; verify newer documentation commits separately. Nothing in P2-A submits signals or trades.
2. **P2-B: build a market-session-aware historical data normalizer.** Strict `interval_ms` contiguity in P2-A rejects overnight/weekend/holiday gaps in real equity price bars. Choose an exchange calendar/timezone, explicit corporate-action adjustment semantics, point-in-time vendor revision rules and missing-bar policy. Add adversarial market session tests before treating AlphArena equities as credible historical research input.
3. Build a documented fixed-rule trend baseline against `AlphaStrategy`, with an explicit, calibratable mapping from historical momentum to a signed *forecast* and a known forecast horizon. Do not equate trailing return with predicted return without justification.
4. Then add fixed momentum/reversion, static ensemble, regime selector, and cost-aware walk-forward evaluation with exact same observation set, leakage restrictions and turnover accounting.

## Acceptance and guardrails

- No future information, including historical revisions with `available_at_ms` after the decision, may affect a past feature.
- Never silently extrapolate, backfill, join incompatible time intervals or align unadjusted with adjusted price series.
- Reproducible inputs must include dataset version, observation/availability timestamps, feature config, and deterministic ID. Keep data-provider source code separate from feature computation.
- `PriceBar` and `FeatureSnapshot` are not executable intents. Risk, portfolio and venue execution remain independent.
- AlphArena is a low-frequency **position API**, not an order-book market maker; SHIFT is the order-level execution validation venue.
- No actual credentialed API calls or trade submissions in these PRs. CI can perform repository's existing read-only jobs but may not authorize trading.
