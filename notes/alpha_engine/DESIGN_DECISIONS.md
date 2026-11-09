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

**Decision:** `alpha/market_data.py` defines frozen `PriceBar` with a completed-bar `end_ms`, the earliest `available_at_ms`, UTC epoch milliseconds, fixed `interval_ms`, `source` (including the data revision identifier), exact Decimal OHLCV and an optional injected `BarProvider` Protocol. CSV ingestion requires explicit columns for both times and explicit source/interval arguments; it does not infer availability, timezones, adjusted prices or corporate actions. Bar publication before its end is invalid.

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
