# Next-agent handoff — shared alpha engine

**Date:** 2026-11-09; **active strategy branch:** `feat/alpha-strategy-baselines`; **base:** `main` with merged P1 [PR #15](https://github.com/kyle-nguyennn/market-maker/pull/15) and P2 [PR #16](https://github.com/kyle-nguyennn/market-maker/pull/16), squash commit `4956b350a34b1640285538a4aa8e6713352679cd`. **Current draft:** [PR #17](https://github.com/kyle-nguyennn/market-maker/pull/17).

## First five minutes

1. Read [README](./README.md), [DESIGN_DECISIONS](./DESIGN_DECISIONS.md), [IMPLEMENTATION_LOG](./IMPLEMENTATION_LOG.md) and `exchanges/alpharena/NOTES.md`.
2. Inspect `model/AGENTS.md`, `alpha/observations.py`, `alpha/calculators.py`, `alpha/features.py`, `alpha/feature_engine.py`, `alpha/signal_engine.py`, `alpha/signal_models.py`, `alpha/strategy.py`, `alpha/strategies.py`, `alpha/strategy_engine.py`, `alpha/pipeline.py`, `quoter/alpha_strategy_adapter.py`, `alpha/STRATEGIES.md` and `tests/test_alpha_strategy_baselines.py`.
3. Get the latest [PR #17](https://github.com/kyle-nguyennn/market-maker/pull/17) head SHA and its GitHub Actions jobs. Test the latest commit; do not reuse earlier successful check results for newer commits.
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

## Priority follow-ups

1. Validate latest PR #17 head CI for all project suites and SDK-only offline tests; review and merge the separate strategy-baselines PR when authorized.
2. **Session-aware equities normalization (P2-B):** current FeatureEngine requires contiguous minute bars and will rewarm across US equity session boundaries. Specify holiday/DST/split/adjustment/revision policies and validate actual Alpaca source semantics before using paper outcomes as research evidence.
3. **Walk-forward evaluation (P3):** compare fixed momentum/reversion/no-trade (then static ensembles) over identical point-in-time data and forecast horizons, with transaction fees, position turnover, slippage and realistic execution assumptions. Never tune on the holdout.
4. **Strategy selection:** only after P3, research regime-aware selection/portfolio composition and route authorized exposure decisions to AlphArena; separately evaluate maker fill quality and adverse selection with SHIFT.

## Acceptance and guardrails

- No future information, including historical revisions with `available_at_ms` after the decision, may affect a past feature.
- Never silently extrapolate, backfill, join incompatible time intervals or align unadjusted with adjusted price series.
- Reproducible inputs must include dataset version, observation/availability timestamps, feature config, and deterministic ID. Keep data-provider source code separate from feature computation.
- `PriceBar` and `FeatureSnapshot` are not executable intents. Risk, portfolio and venue execution remain independent.
- AlphArena is a low-frequency **position API**, not an order-book market maker; SHIFT is the order-level execution validation venue.
- No actual credentialed API calls or trade submissions in these PRs. CI can perform repository's existing read-only jobs but may not authorize trading.
