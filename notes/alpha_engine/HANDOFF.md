# Next-agent handoff — shared alpha engine

**Date:** 2026-11-09; **active P3 backtest branch:** `feat/alpha-p3-causal-backtest`; **base:** `main` with P1 [PR #15](https://github.com/kyle-nguyennn/market-maker/pull/15), P2 [PR #16](https://github.com/kyle-nguyennn/market-maker/pull/16), and merged P2-C [PR #17](https://github.com/kyle-nguyennn/market-maker/pull/17), squash commit `5bf9fb77fec846ca92e8b92e72c1928b18a247f0`. **Current draft:** [PR #18](https://github.com/kyle-nguyennn/market-maker/pull/18).

## First five minutes

1. Read [README](./README.md), [DESIGN_DECISIONS](./DESIGN_DECISIONS.md), [IMPLEMENTATION_LOG](./IMPLEMENTATION_LOG.md) and `exchanges/alpharena/NOTES.md`.
2. Inspect `model/AGENTS.md`, `alpha/observations.py`, `alpha/calculators.py`, `alpha/features.py`, `alpha/feature_engine.py`, `alpha/signal_engine.py`, `alpha/signal_models.py`, `alpha/strategy.py`, `alpha/strategies.py`, `alpha/strategy_engine.py`, `alpha/pipeline.py`, `quoter/alpha_strategy_adapter.py`, `alpha/STRATEGIES.md` and `tests/test_alpha_strategy_baselines.py`.
3. Get the latest [PR #18](https://github.com/kyle-nguyennn/market-maker/pull/18) head SHA and its GitHub Actions jobs. Test that commit; do not treat earlier green checks as valid for a newer head.
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

## Official Alpaca SDK host-side integration (ADR-015)

- `market_handler/alpaca_stock_market_data.py` contains two adapters:
  `AlpacaHistoricalBarProvider` (historical minute BarSet → PriceBar,
  explicit assumed publication lag) and `AlpacaMinuteBarStream` (SDK
  `StockDataStream.subscribe_bars` async callbacks → PriceBar with actual
  callback receipt time). SDK is optional via `requirements/alpaca.txt`
  pinned to `alpaca-py==0.45.0`; `alpha/` imports none of it.
- Normalize SDK `Bar.timestamp` (opening timestamp) to completed
  `PriceBar.end_ms` by adding 60_000 ms. Historical endpoints cannot supply
  real first-publication timestamps or revision logs; an explicitly assumed
  `assumed_publication_lag_ms` and source/version identity are mandatory.
- SDK Bar OHLCV properties are normally floats. `Decimal(str(value))`
  preserves the SDK value text but cannot recreate precision lost before
  conversion. Do not use this path as an L2/tick-precision source.
- `market_handler/ALPACA.md` has offline/historical/live usage examples.
  No credentials or API calls in tests; live `run()` must be explicitly
  invoked and is blocking. Updated-bar corrections and market calendars,
  reconnect and persisted replay remain future work.
- `tests/test_alpaca_market_data.py` uses mocked historical/stream clients;
  `.github/workflows/alpaca-market-data-tests.yml` installs the real SDK
  for constructor/request-model compatibility (no network/API keys).
- Pinned SDK runs caught three subtle assumptions: `Sort` is in
  `alpaca.common.enums`; `TimeFrame` must be compared by amount/unit (not
  Python object equality); and SDK stock requests normalize aware UTC
  datetimes to naive UTC values. Adapter and contract assertions corrected.
- Latest implementation CI: [Alpaca SDK #42595956921](https://github.com/kyle-nguyennn/market-maker/actions/runs/42595956921), **17 passed** (real SDK request and stream constructor); [full project #42595957138](https://github.com/kyle-nguyennn/market-maker/actions/runs/42595957138), **353 passed, 2 skipped** in each of default and PAS, scored PAS read-only success. No external Alpaca request was attempted. Re-check the final documentation-only head before merge.
- AlpArena does not supply a documented public equities OHLCV feed; use this
  Alpaca data adapter for its independent features, while preserving the
  separate paper-position AlpArena execution API.

## Strategy layer P2-C (current draft PR #17; ADR-016/017)

- `alpha/signal_models.py` implements two **explicitly uncalibrated**
  deterministic forecast hypotheses against the P2 price feature schema:
  `MomentumSignalModel` forecasts `k * momentum` and
  `MeanReversionSignalModel` forecasts `-k * price_zscore * realized_vol`
  with `realized_vol` measured per bar. Required `feature_version`,
  `model_version`, positive coefficient and `horizon_ms` are enforced;
  `min_abs_momentum`/`min_abs_zscore` support abstention.
- `model_score` (forwarded as `AlphaSignal.confidence`) is a **fixed
  uncalibrated score, NOT a win probability**. Signals have deterministic
  SHA-256 IDs referencing exact snapshot/config/time. Never claim alpha
  profitability from these reference models without walk-forward validation.
- `alpha/strategy_engine.py` runs registered `Strategy` classes on
  **one** `StrategyContext`, returning a `StrategyBatch` of separate
  `StrategyDecision` values. This allows parallel comparison of
  `MomentumStrategy`, `MeanReversionStrategy` and the host
  `MarketMakingStrategy` adapter. It is NOT a portfolio combiner,
  automatic selector, risk authorizer or trading executor.
- StrategyEngine verifies ownership, symbol, signal lineage and time.
  Invalid market and optional max-age gates cause explicit batch abstention.
  Portfolio/market time and freshness remain incomplete without a
  live risk and reconciliation layer.
- `AlphaPipeline` continues to support a **single** strategy and can
  run the real models through identical replay/live bar paths; the batch
  evaluator is explicitly separate for research.
- Test file: `tests/test_alpha_strategy_baselines.py`. Initial [full CI
  #42597417136](https://github.com/kyle-nguyennn/market-maker/actions/runs/42597417136)
  passed **388 tests, 2 skipped** twice; [Alpaca SDK
  #42597417402](https://github.com/kyle-nguyennn/market-maker/actions/runs/42597417402)
  passed 17. Check latest PR head after documentation commits.
- **Next immediate objective:** session-aware equities normalizer, followed
  by point-in-time walk-forward comparison of fixed momentum/reversion/no-trade
  policies with fees, turnover, slippage and benchmark. Avoid implementing
  a regime-aware policy selector on unmeasured baselines.

## P3-A offline causal backtesting (current PR #18, ADR-018)

- New host-side `backtest/` package, not merged or imported by `alpha/`.
  The existing `AlphaPipeline.on_bar` and `Strategy.decide` remain the
  only strategy decision contracts. This is a **single asset, single policy**
  simulator for desired target exposures, not an order router.
- `CausalBarBacktester` MUST process each historical bar as **two separate,
  globally ordered events**: OPEN at `end_ms - interval_ms`, publication
  at `available_at_ms`. OPEN precedes publication on ties.
  **Regression rationale:** when bar N+1 opens at 10:02:00 but bar N
  publishes at 10:02:01.500, a decision made by the latter cannot alter
  the fill already simulated at 10:02:00. Processing complete bars one
  by one violates this. A test explicitly catches cancellation after
  the opening.
- Fills only occur at the first future opening satisfying
  `decision_at + latency <= open_time <= expiry`, with expiry limited
  by configured order TTL and target forecast horizon. Cash/position
  ledger uses fixed Decimal, average entry, realized/unrealized P&L,
  fee/slippage, target limits and quantity step. Same pending target
  does not restart latency. Abstention means hold, not flatten.
- Mark account equity at bar publication using **chronologically freshest
  observed open or published close**, not automatically that bar's close.
  Some next-minute opens are visible before the previous bar's delayed
  publication; a future BAR CLOSE is still unavailable. `EquityPoint`
  records both `close_price` and `mark_price`.
- `apps/backtest_alpha.py` has local normalized CSV replay and optional
  *read-only* Alpaca historical fetch, with momentum/reversion/cash CLI
  family, explicit model and cost parameters, JSON metrics and optional
  fill/equity CSV exports. `backtest/README.md` documents commands,
  examples, bias limitations; `backtest/AGENTS.md` states invariants.
- Tests `tests/test_alpha_causal_backtest.py` and
  `tests/test_alpha_backtest_cli.py` cover causal chronology, short/long
  accounting, historical future-close mutation, realistic order expiry,
  cost reconciliation, no-trade and deterministic CSV report.
- [Full CI #42601233788](https://github.com/kyle-nguyennn/market-maker/actions/runs/42601233788):
  **423 passed, 2 skipped** in both default and PAS configurations, PAS
  read-only successful, no trading. Confirm latest PR #18 HEAD after docs.
- **No claim of true PIT/backtest performance:** Alpaca historical
  publication-lag is assumed. Bar-open fills are an idealized price model,
  not evidence of actual access to that liquidity. No L2 maker fills,
  borrow/financing/dividends, slippage calibration or partial fills.
- **Immediate next milestone:** US equity session/no-trade interval
  normalization and walk-forward comparator with frozen parameters,
  transaction-cost sensitivity and a buy-and-hold control.

## Priority follow-ups

1. Verify [PR #18](https://github.com/kyle-nguyennn/market-maker/pull/18) latest-head CI and review its simulated fill/mark chronology. Leave draft until reviewed/explicitly merged.
2. **P2-B US equity session policy:** `FeatureEngine` still rejects gapped 1-minute bars; add calendar/DST, halts, market closures, non-trading minutes, historical revisions, split/dividend availability and post-gap warmup semantics.
3. **P3-B walk-forward comparison:** frozen-parameter momentum/reversion/cash/buy-hold controls using identical point-in-time feature streams, training/validation/test separation, fee/slippage sensitivity and exposure/turnover metrics. No parameter fitting on holdout.
4. **Later:** multi-symbol risk portfolios, liquidity-aware partial-fill model, controlled AlphArena paper forward-testing and SHIFT order-book/maker-fill simulation separately. Nothing in PR #18 sends trades.

## Acceptance and guardrails

- No future information, including historical revisions with `available_at_ms` after the decision, may affect a past feature.
- Never silently extrapolate, backfill, join incompatible time intervals or align unadjusted with adjusted price series.
- Reproducible inputs must include dataset version, observation/availability timestamps, feature config, and deterministic ID. Keep data-provider source code separate from feature computation.
- `PriceBar` and `FeatureSnapshot` are not executable intents. Risk, portfolio and venue execution remain independent.
- AlphArena is a low-frequency **position API**, not an order-book market maker; SHIFT is the order-level execution validation venue.
- No actual credentialed API calls or trade submissions in these PRs. CI can perform repository's existing read-only jobs but may not authorize trading.
