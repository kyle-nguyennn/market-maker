# Shared alpha engine — architecture decision record

Last updated: **2026-11-09**. P1 decisions remain authoritative; P2 additions are recorded below. Add new dated ADRs; do not silently overwrite old reasoning when requirements change.

## ADR-001 — Separate alpha contracts from market-making order contracts

**Status:** Accepted. **Decision:** Introduce pure `alpha/` package; keep `model/order.py`, `quoter/BaseMMQuoter` and `execution/order_manager.py` untouched. Consume `TargetExposure` at venue-specific translator layers later.

**Why:** AlphArena opens/closes positions without resting orders. SHIFT supports order placement and maker quoting. A shared order representation would leak execution assumptions into research features.

**Alternative rejected:** Reuse `OrderIntent` or make `BaseMMQuoter` output position ops. This conflates portfolio intent, order placement and fills.

## ADR-002 — Signal, feature, and portfolio target are different objects

**Status:** Accepted. **Decision:** P1 exports `FeatureSnapshot`, `AlphaSignal`, `TargetExposure` and structural `AlphaStrategy` Protocol. Strategies return a forecast or abstain; **portfolio construction** decides target exposure separately.

- `AlphaSignal.expected_return` is a signed fractional forecast (e.g. 0.002 is +0.2%), not a trade probability or position weight.
- `TargetExposure.target_weight` is signed fraction of **account equity** (e.g. -0.10 is 10% short). May be >1 in the pure model because leverage restrictions belong to risk.
- `confidence` is a Decimal model score on [0,1], **not assumed calibrated**.
- `signal_ids` supports ensemble provenance.

**Alternative rejected:** Strategies return buy/sell orders or venue position sizes. This prevents comparison across simulated and platform executions.

## ADR-003 — Temporal availability and lineage are first-class

**Status:** Accepted. **Decision:** `FeatureSnapshot` has `observed_at_ms`, `available_at_ms`, lookback, source, feature version and snapshot ID. `AlphaSignal` has generated time, feature-availability time, forecast horizon, model version and source snapshot ID; signal generation before feature availability is invalid. `TargetExposure` records decision time, horizon, strategy ID and signal IDs.

**Why:** Observation timestamp does not imply tradable/publication availability. Data latency and as-of reproducibility are critical to walk-forward leakage control.

**Deferred:** Cross-object checks for matching symbols, snapshot IDs and horizon compatibility belong to the future strategy-evaluation / selector pipeline, which holds all objects. A dataclass alone cannot validate other objects' contents.

## ADR-004 — Exact Decimal and immutable snapshots

**Status:** Accepted. **Decision:** Use only finite `Decimal` for continuous financial/numeric values; reject floats, NaN, Infinity, booleans in millisecond timestamp fields. Feature mappings are copied into read-only sorted mappings at construction; types are frozen dataclasses.

**Why:** Matches existing `model/AGENTS.md` and prevents retroactive mutation of logged feature inputs.

**Consequence:** `MappingProxyType` is not directly JSON-serializable and `dataclasses.asdict` will not serialize it without an explicit converter. A future journal codec must serialize Decimals and feature mappings deliberately. Do not add a generic float-based serializer.

## ADR-005 — No risk limits / policies in P1

**Status:** Accepted. **Decision:** No alpha model, regime classifier, strategy selector, backtest, scheduling, account reconciliation or broker adapter in this PR. No venue-specific maximum target weight and no implicit flattening. Only positive forecast horizon and timestamp integrity are checked.

**Why:** P1 is a domain compatibility foundation, not proof of profitability or executable risk safety.

**Deferred / open design decisions:**
- Model higher-dimensional / multi-symbol features and trading calendars (current P1 snapshot is single-symbol).
- Confidence calibration and choice of probability semantics.
- Portfolio construction gross/net/sector constraints, offsetting forecasts, and ensemble weighting.
- How to evaluate incompatible signal horizons and sampling frequencies.
- Return label convention (simple vs log returns); **P1 expected_return uses fractional simple return** by convention for the pilot, but verify evaluator consistency.
- Persisted serialization schema, timestamps/clock domain and feature-cache lifetimes.
- SHIFT quote-skew mapping and hedging, with separate fill/adverse-selection validation.

## ADR-006 — Offline historical bars have two clocks and explicit dataset identity

**Date:** 2026-11-09. **Status:** Accepted for P2-A.

**Decision (original P2-A, superseded for file ownership by ADR-014):** `alpha/market_data.py` defines frozen `PriceBar` with a completed-bar `end_ms`, the earliest `available_at_ms`, UTC epoch milliseconds, fixed `interval_ms`, `source` (including the data revision identifier), exact Decimal OHLCV and an optional injected `BarProvider` Protocol. CSV ingestion requires explicit columns for both times and explicit source/interval arguments; it does not infer availability, timezones, adjusted prices or corporate actions. Bar publication before its end is invalid.

**Reason:** A historical last-trade timestamp is not a proof that a trading agent could have observed that bar. Publication lag, revisions, timezone conversions and corporate-action adjustment can introduce leakage. Dataset provenance must be supplied by the owner of a downloaded feed, not guessed by an exchange adapter.

**Alternatives rejected:** Infer availability from bar end; use system time; call a live stock-data API during an offline replay; silently convert float CSV prices.

## ADR-007 — Fail closed on duplicate revisions, missing bars, and staleness

**Date:** 2026-11-09. **Status:** Accepted for P2-A.

**Decision:** `build_price_features()` accepts an explicit `decision_at_ms`, selects only rows with `available_at_ms <= decision_at_ms`, sorts eligible rows deterministically, rejects duplicate end timestamps (unresolved revisions), and uses the last `lookback_bars + 1` completed observations. Require same source, same interval, and exact contiguous UTC-time spacing in that active window. Refuse stale data using a configured `max_staleness_ms`; never interpolate or backfill a gap.

**Reason:** Trading calendar and vendor revision semantics are not yet implemented; a fail-closed replay makes assumptions visible rather than generating spurious alpha.

**Limitation / explicit follow-up:** Strict wall-clock contiguity rejects many real stock/ETF histories across weekends, overnight closures and holidays. Before AlphArena research on equities, introduce a tested exchange-calendar/session-aware normalizer (and documented corporate-action policies). A later bitemporal revision store may select the latest *as-of-known* revision per end timestamp, but do not silently choose between duplicates in P2-A. Market-data sourcing, licensing and vendor-specific semantics remain unimplemented.

## ADR-008 — Stable point-in-time feature identity and calculation conventions

**Date:** 2026-11-09. **Status:** Accepted for P2-A.

**Decision:** `alpha/features.py` exports `FeatureConfig` and `build_price_features` (strictly offline). A SHA-256 snapshot ID hashes canonical JSON of requested decision time, config, input source, availability timestamps and all consumed OHLCV fields; unrelated future observations are excluded from the hash and calculation. No random inputs, local path, wall-clock timestamps or Python `hash()`. Decimal arithmetic uses a fixed precision of 34 digits and `ROUND_HALF_EVEN` independent of the caller's context.

Feature names / units:
- `return_1`: last close-to-close **simple fractional** return.
- `momentum`: last close / first close - 1 over `lookback_bars` intervals.
- `realized_vol`: **population** standard deviation of those `lookback_bars` simple one-bar returns, **not annualized**.
- `price_zscore`: latest close against the last `lookback_bars` closes, population deviation, **zero when variance is zero**.

`FeatureSnapshot.observed_at_ms` is the newest completed bar end; `available_at_ms` is the latest availability among consumed observations; `lookback_ms` is `interval_ms * lookback_bars`.

**Trade-off:** P2-A uses a simple research baseline rather than full corporate-action/time-series semantics. A hash identifies a particular as-of computation but is not itself proof a vendor supplied unmodified historical data. No forecasts, portfolio targets, orders, or backtest P&L are generated.

## ADR-009 — Keep fixed strategies and cost-aware selection in subsequent slices

**Date:** 2026-11-09. **Status:** Deferred work, not implemented in P2-A.

Implement one conservative fixed-rule `AlphaStrategy` baseline against `momentum` only after P2-A contract and CI review. Define a forecast horizon and a *documented, calibratable mapping* from descriptive trailing return to expected future return; do not assert that trailing return itself is statistically unbiased forecast. Add the fixed mean-reversion, momentum, static ensemble, regime selector and cost-aware walk-forward evaluator in independent, tested increments. No AlpArena signal submission until portfolio safety/reconciliation and dry-run controls are developed.


## ADR-010 — Vendored-for-now, independently publishable `alpha/` package

**Date:** 2026-11-09. **Status:** Accepted.

**Decision:** Keep feature calculation, rolling FeatureEngine, SignalEngine, Strategy contracts and baseline directional policies under `market-maker/alpha/` for now. Treat that directory as a standalone standard-library-only package that could later be moved to a separate repository or distributed artifact without rewriting the public interface. No imports from `model/`, `quoter/`, `execution/`, `exchanges/`, Redis or SDK packages are allowed inside `alpha/`. Bridge to existing market-maker types in `quoter/alpha_strategy_adapter.py`, not inside the publishable package.

**Rationale:** Fast iteration inside the active competition repository while preserving an extraction seam for a future feature/signal library. We are explicitly **not** migrating it to `market-ai-platform` now.

**Consequence:** `alpha/strategy.py` defines a narrow structural `MarketView` protocol and immutable `PortfolioSnapshot`, not concrete `model.MarketState` or `portfolio.PortfolioState` types. This may produce small adapter objects and duplicate immutable views; avoid hidden SDK dependencies just to reduce adapters. A future extraction needs its own package metadata, semantic versioning and serializable schemas, not a blind directory copy.

## ADR-011 — Separate pure calculation, rolling feature state, and signal inference

**Date:** 2026-11-09. **Status:** Accepted.

**Decision:**
- `PriceFeatureCalculator` in `alpha/calculators.py` is a pure, replaceable numerical calculator implementing `FeatureCalculator.calculate(history)`. Calculation contains no feed clock, history storage, I/O or trading decisions. Existing `build_price_features` retains P2-A point-in-time validation, provenance and snapshot assembly and now delegates to that calculator.
- `FeatureEngine` in `alpha/feature_engine.py` owns one symbol's bounded rolling OHLCV window, source/interval identity, ordering, warmup, missing-bar invalidation and as-of lookup. It emits immutable `FeatureSnapshot` objects.
- `SignalEngine` in `alpha/signal_engine.py` owns registered `SignalModel` predictors and checks generated outputs against feature snapshot ID, symbol, feature availability, generation timestamp and model identity. It publishes immutable `AlphaSignal` tuples. It is not a research feature store or trading executor.

**Rationale:** Cheap incremental observation ingestion should not be coupled to prediction policy; pure calculation must be equally testable in replay and in live mode. New calculators or inference models can be swapped independently.

**Trade-off:** For correctness simplicity, the current engine recomputes from its bounded rolling window on each completed bar rather than maintaining O(1) update formulas. Optimization can follow profiling and reference-baseline equivalence tests. P2 price features remain deliberately simple reference statistics, not finished alpha research.

## ADR-012 — One market-data ingestion API for replay and live

**Date:** 2026-11-09. **Status:** Accepted.

**Decision:** Both live and replay producers call `FeatureEngine.on_bar(PriceBar, at_ms=...)`; a composed `AlphaPipeline.on_bar(...)` invokes FeatureEngine -> SignalEngine -> Strategy and returns a non-executable `PipelineResult`. `replay_bars` / `replay_pipeline` are thin adapters that sort historical bars by **publication availability**, and invoke those exact live methods. No `is_backtest`, `is_live` branch, history-store reference or clock read inside strategies.

**Quality semantics:** Reject out-of-order/duplicate end times, unavailable publications, backward arrival clocks, mixed symbol or source versions; on a completed-bar interval gap, **discard the rolling window and require fresh warmup**. Retain explicit gap counters. Latest snapshots still validate staleness. Any feature-dependent strategy must abstain during warmup.

**Known limitation:** The current event input is a *completed price bar* only, not a generalized L2 or trade event. High-frequency SHIFT features (order flow, microprice, queue dynamics) need a separate typed observation/calculator family behind the same conceptual ingestion boundary. Runtime assumes one producer per engine; concurrency/lock-free hot-path handling is not yet validated. Delayed arrival that reorders historical bar ends fails closed. Real equity sessions require the planned calendar-aware normalizer before viable AlphArena studies.

## ADR-013 — Strategy generalizes MM quoter and directional policies

**Date:** 2026-11-09. **Status:** Accepted.

**Decision:** `SignalModel.predict(features, generated_at_ms)` is the new pure forecasting interface; retain P1's `AlphaStrategy.generate_signal(snapshot)` Protocol for compatibility, but do not implement new policy classes against it. `Strategy.decide(StrategyContext)` is the common policy contract. `StrategyContext` contains as-of-aligned features, signals, an optional venue-neutral market view and optional immutable portfolio view.

`StrategyDecision` can contain a tuple of desired `TargetExposure` values (directional families), a tuple of non-executable `QuoteProposal` values (market-making), or both empty to abstain. Mixing exposures and quotes in the same policy return is initially forbidden. A policy does **not** place orders, send positions, size to venue precision, or bypass downstream risk controls.

**Compatibility:** `quoter/alpha_strategy_adapter.py` adapts existing `BaseMMQuoter.quote(MarketState)` to `Strategy.decide(context)`, and also supports `quote_context(context)` for future alpha-aware MM quoters that consume full feature/signal/portfolio inputs. `MomentumStrategy` and `MeanReversionStrategy` are initial fixed-threshold, fixed-target-weight *policy skeletons* consuming forecasts selected by `SignalEngine`. They are not fitted predictive models and no profitability is claimed.

**Next:** Define a multi-strategy regime selector/compositor, portfolio-level cross-symbol construction, market-context freshness/revisions, live feed bridge and offline transaction-cost evaluator separately. Do not make `BaseMMQuoter` inherit a strategy whose domain contract imports market-maker-specific Quote objects.


## ADR-014 — Host owns market-data ingestion; alpha owns normalized observation contracts

**Date:** 2026-11-09. **Status:** Accepted; supersedes file ownership in ADR-006 while preserving its bar and timestamp semantics.

**Decision:** `alpha/market_data.py` was removed. Keep the immutable `PriceBar` and read-only `BarProvider` Protocol in `alpha/observations.py` as **input contracts only**. The concrete, strict Decimal-preserving `read_price_bars_csv` function now lives in `market_handler/historical_bar_reader.py`. All alpha calculators, `FeatureEngine`, and `AlphaPipeline` consume `PriceBar` via `alpha.observations` and have no knowledge of whether it came from CSV, external data provider, or an eventual live venue adapter.

**Dependency direction:** host data sources → `alpha.observations` → `FeatureEngine` → `SignalEngine` → `Strategy`. `alpha/` must not import `market_handler/`, `model/`, `quoter/`, or any venue SDK. Keep later Parquet readers, feed subscriptions, symbol mappings, exchange/session calendar normalization, and source-specific integrity outside `alpha/`. Existing `model.MarketState` represents an L2 book, **not** an OHLCV `PriceBar`; do not silently substitute one for the other.

**AlphArena decision:** `exchanges/alpharena/client.py` remains a paper-position and performance API client, **not** a market-data-source handler. The documented public `alpharena.io/api/v1` endpoints cover profiles, portfolios, trades, P&L, leaderboard, strategies and trade mutations; no documented OHLCV/bar stream was identified as of 2026-11-09. It uses market prices internally to mark/fill simulated positions, which does not imply its API exposes those prices as a research feed. For AlphArena equities, first provide a suitable independently sourced historical/live equities feed via a host-level adapter returning normalized `PriceBar`. Do **not** create an `AlphArenaMarketDataHandler` unless a real separate data endpoint is documented. Unlike order-level SHIFT simulation, AlphArena can be driven by low-frequency alpha features and account/position data.

**Alternatives rejected:** `alpha/` importing concrete CSV/Redis readers, conflating Hyperliquid L2 market-data handler with an AlpArena stock feed, or implementing an imaginary venue market-data API.

**Evidence:** `tests/test_market_data_boundary.py` checks identical replay/live feature behavior from host-ingested bars and guards the package-import boundary. `tests/test_alpha_features.py` continues validating CSV fidelity and as-of computation after the move.

**Known follow-up:** `BarProvider` is only a source-port Protocol; this PR ships a CSV function, not a full provider implementation, async market-data bus, or source registry. Venue-neutral multi-feed subscription, equity exchange calendars and controlled historical publication lags are separate tasks.


## ADR-015 — Official Alpaca SDK as optional US-equities bar source

**Date:** 2026-11-09. **Status:** Accepted.

**Decision:** Integrate official `alpaca-py==0.45.0` in **host-side** `market_handler/alpaca_stock_market_data.py` with independent `AlpacaHistoricalBarProvider` and `AlpacaMinuteBarStream`. Optional dependency is isolated in `requirements/alpaca.txt`; importing `alpha/` or the market-data adapter does not import `alpaca-py` unless an SDK client/request is explicitly constructed. No Alpaca TradingClient, order API, or AlphArena trading API calls.

**Shared contract:** Both sources emit `alpha.observations.PriceBar`, with exact integer UTC timestamps and Decimal OHLCV. Alpaca `Bar.timestamp` is **bar start**; normalize `end_ms = start_ms + 60_000`. SDK numeric fields may already be Python floats; use `Decimal(str(value))` and document that wire precision previously lost by the SDK cannot be restored.

**History:** `StockHistoricalDataClient.get_stock_bars(StockBarsRequest(...))` using explicit feed (default IEX), adjustment (default RAW), configured history start, SDK Minute timeframe and user-provided `until_ms`. Historical Alpaca bars **do not** report when that specific price/adjusted value was first published or revised. Require an explicit `assumed_publication_lag_ms` and encode it, feed, adjustment and caller-specified `source_version` in `PriceBar.source`. Exclude bars not complete or not available under that assumed lag as of requested time. This is **not** historically observed PIT availability and must not be claimed as such.

**Live:** `StockDataStream.subscribe_bars(async_handler, *symbols)`; `run()` starts an **explicit blocking** WebSocket loop. Normalize only completed bars and set `available_at_ms` to caller-measured UTC callback receipt. Forward through a caller-supplied asynchronous `on_bar(PriceBar, at_ms)` that may pass directly to `FeatureEngine`/`AlphaPipeline`. No strategy-visible feed flag. **Do not** subscribe to `subscribe_updated_bars` until a revision policy can reconcile an already-published bar without look-ahead or silently rewriting feature lineage. Live and history source identities are intentionally distinct to prevent implicit mixed provenance in one rolling engine.

**Non-goals:** Alpaca paper/live trading, Alpaca account reads, AlphArena order/position submissions, durable historical capture, order-book microstructure, exchange-calendar adjustment, reconnection/backpressure controls and actual SDK live/historical key-backed requests. Stale/missing minute bars still force FeatureEngine warmup per ADR-011; a dedicated session-aware normalizer is required for US equity overnight/weekend/holiday boundaries.

**Validation:** `tests/test_alpaca_market_data.py` provides network-free fake SDK historical/live parity tests and a real-import SDK request-model smoke test that runs only when `alpaca-py` is installed. A dedicated workflow installs the pinned real SDK but makes no remote/API/authenticated requests. Never treat a green mock/SDK constructor test as a proof of live source behavior or historical adjustment accuracy.

**Alternatives rejected:** Treating AlpArena as a stock-data vendor without a documented market feed; mixing historical inferred availability with measured live receive timestamps; making the alpha package depend directly on Alpaca SDK or its floats; wiring the trading SDK into a market-data handler.


## ADR-016 — Baseline SignalModels are explicit uncalibrated forecast hypotheses

**Date:** 2026-11-09. **Status:** Accepted; strategy-layer PR #17 following merged PR #16.

**Decision:** Add `MomentumSignalModel` and `MeanReversionSignalModel` under `alpha/signal_models.py`, keeping numerical indicators in `FeatureCalculator`, forecast hypotheses in `SignalModel.predict`, and trading-policy decisions in `Strategy.decide`. The fixed momentum baseline forecasts a signed future *fractional return* as `forecast_multiplier * trailing_momentum`. The fixed reversion baseline forecasts `-forecast_multiplier * price_zscore * realized_vol`, where `realized_vol` is a **per-bar fractional** standard deviation. These formulae are *declared research hypotheses*, **not** calibrated conditional expectations or evidence of predictive power. Forecast horizon is explicit and need not equal input lookback.

Require each model's `feature_version`, `model_version`, `horizon_ms`, positive finite `forecast_multiplier` and optional minimum feature thresholds. Reject wrong feature versions, missing inputs, unobservable future features or invalid parameters; abstain when the indicator is too small or reversion volatility is zero. Fix numeric computations to a 34-digit Decimal context, hash canonical snapshot/config/model/clock/forecast data into reproducible signal IDs, and retain signal lineage to `FeatureSnapshot.snapshot_id`.

**Confidence semantics:** `model_score` is presently a **fixed, uncalibrated** value in [0,1] passed to `AlphaSignal.confidence`. It is *not* a probability of a winning trade, and must not be described as such. No hidden fitting, black-box selectors, threshold optimization or test-set leakage is introduced.

**Deferred:** point-in-time training labels, statistically estimated coefficients, uncertainty calibration, cross-validation, costs, regime interactions, formal significance testing and live data quality. Avoid using P2-C signal values as justification for actual trading.

## ADR-017 — StrategyEngine is a research fan-out, not a multi-strategy executor

**Date:** 2026-11-09. **Status:** Accepted.

**Decision:** `alpha/strategy_engine.py` defines a `StrategyEngine` registry that evaluates independent `Strategy` subclasses against one `StrategyContext`. Its `StrategyBatch` contains **separately attributed** `StrategyDecision` values. It can compare momentum, mean-reversion and existing MMQuoter-wrapped market-making policies without coercing quote proposals into target exposures or implicitly combining competing trades. It is intentionally **not** a selector, capital allocator, merged portfolio, order router, or trading authorization.

Enforce unique policy IDs, exact decision timestamps/strategy identity, symbol consistency, exposure ownership and membership of every output signal ID in the supplied `StrategyContext`. Quotes require an actual market view. An explicitly invalid market causes whole-batch abstention. The caller may optionally set freshness gates for feature publication, market timestamp and portfolio timestamp; exceeding one records a `skipped_reason` with an empty proposal from each registered strategy. The caller must not infer that unset gates have been validated by a risk manager.

**Rationale:** Our previous architectural agreement requires one `Strategy` abstraction for both resting-order MM and directional exposure policies. Fair offline comparisons must share feature and forecast availability while keeping incompatible execution metrics distinct. A future selector should only be designed after fixed baselines and transaction-cost/fill metrics have been collected.

**Trade-off:** `AlphaPipeline` remains a single-strategy feature→signal→decision composition. The research `StrategyEngine` consumes the exact same `StrategyContext` and can be used by later backtest/evaluation infrastructure. No new storage, websocket handlers, strategy-visible live/replay flags, SDK dependencies or venue dispatch in `alpha/`.

**Next:** Add session-aware equity bar normalization and an executable-free walk-forward evaluator comparing fixed-strategy, no-trade and static-ensemble baselines on identical as-of data. Only then implement regime-aware switching and risk-constrained portfolio composition. MM strategies require order-book fill/adverse-selection evaluation on SHIFT rather than relying on AlphArena paper-position P&L.


## ADR-018 — Offline causal bar-event simulator reuses live alpha/Strategy contracts

**Date:** 2026-11-09. **Status:** Accepted for draft P3 [PR #18](https://github.com/kyle-nguyennn/market-maker/pull/18), based on merged P2-C PR #17.

**Decision:** Implement research backtesting in a new **host-owned** `backtest/` package, not in the independently publishable `alpha/` library and not in the existing venue execution modules. Reuse the exact `AlphaPipeline.on_bar`, `SignalModel.predict`, `Strategy.decide`, `TargetExposure`, `PriceBar` and immutable provenance contracts. No `is_backtest` branches in a strategy, venue SDK imports, account reads or broker-order submission. Initial engine is single-asset/single-policy only and refuses `QuoteProposal` because resting maker quotes cannot be valued from OHLCV alone.

**Global event-time sequence (critical):** Every historical bar becomes TWO distinct replay events: (1) an OPEN event at `bar.end_ms - bar.interval_ms`, and (2) a PUBLICATION event at `bar.available_at_ms`. Process events **globally sorted by time**, with opening before publication on timestamp ties. At OPEN, execute a previously scheduled exposure intent only when `open_ms >= decision_at_ms + latency_ms` and `open_ms <= expires_at_ms`. At PUBLICATION, mark equity using the chronologically freshest observable open/close, create an immutable current `PortfolioSnapshot` (marked equity and signed holdings), then publish the completed OHLCV and that portfolio view into the existing `AlphaPipeline.on_bar` / `StrategyContext` before creating new future intents from Strategy decisions.

**Why split events:** With ordinary US-equity minute bars, the next minute can open at 10:02:00 while the previous completed bar is not published until 10:02:01.500. A per-completed-bar loop would process the later signal **before** an earlier open and could cancel a trade retroactively. The full event sequence prevents that. A normalized bar's future close/high/low is inaccessible to signal computation before its actual publication; its open is only used for hypothetical order execution/marking at its opening time. A publication-late *close* must not overwrite a more recent already-observed market opening price used for portfolio mark-to-market.

**Execution/portfolio:** `CausalBarBacktester` converts a bounded `TargetExposure` to a hypothetical signed quantity at the **first eligible future bar-open reference price**, adverse static slippage and proportional fee; uses explicit Decimal quantity step, max absolute target weight, rebalance threshold, latency and TTL/horizon expiry. Repeated equal pending targets do not renew their original eligibility/TTL, preventing starvation. Empty decisions mean hold/abstain, not automatic liquidation. Tracks signed position/average-entry cost, cash, price P&L on reductions/flips, unrealized marked P&L, total fees/slippage, turnover and equity curve. Performs daily returns grouped by America/New_York (DST-aware) and reports exploratory sample-Sharpe only when sufficient days exist.

**Interpreting fill model:** This is a causally scheduled but **idealized** bar-open market fill; it does not establish venue fill probability, spread, impact, partial fills, stops, order rejection, borrow fees, financing, dividends, taxes, trading hours or corporate actions. The actual bar-open execution might be unavailable in a real book, even when the decision precedes it. Slippage/fees are scenario parameters, not historical measurements. `BarProvider` historical Alpaca publication timing is an explicit assumption and not point-in-time certified. No claim of realized profit is justified without session-aware normalization, true publication/revision history, realistic execution and out-of-sample evaluation.

**Interfaces:** `apps/backtest_alpha.py` permits local normalized CSV replay and, when explicitly invoked by the user, read-only historical Alpaca data via the existing host adapter. It prints JSON metrics and optionally exports an equity/fill ledger. Cash-only/no-trade mode is available as a control. The backtest module is single-use to avoid hidden state reuse between experiments; each strategy/comparator needs a new pipeline and simulator.

**Validation:** `tests/test_alpha_causal_backtest.py` and `tests/test_alpha_backtest_cli.py` exercise exact ledger/cost identities, horizon/TTL, signal latency and future-open causality, retroactive-cancellation regression, future-close invariance, observable mark precedence, maker-quote rejection, strict bar clocks, no-trade baseline and end-to-end CSV CLI. Source tests are wholly offline and do not touch Alpaca account keys, AlphArena paper execution or actual live trades.

**Next:** P2-B session/calendar-aware equity normalization and P3-B frozen-parameter, fee/turnover-aware walk-forward comparators (cash, momentum, reversion, passive buy/hold, ensemble) on matched point-in-time data. Multi-symbol portfolios, partial fills and maker microstructure belong in later dedicated engines.
