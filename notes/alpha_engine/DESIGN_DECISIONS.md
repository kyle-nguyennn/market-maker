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
