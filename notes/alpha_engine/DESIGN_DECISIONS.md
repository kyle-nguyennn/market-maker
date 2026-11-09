# Shared alpha engine — architecture decision record

Last updated: **2026-11-09**. Decisions below apply to P1 only unless explicitly noted. Add new dated ADRs; do not silently overwrite old reasoning when requirements change.

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
