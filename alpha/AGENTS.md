# Alpha package invariants — vendored, library-ready

This package temporarily lives in `market-maker` but is designed to be
publishable as an independent Python library. Preserve a clean dependency
boundary and keep strategy execution and venue wiring outside this directory.

1. **No imports from other project packages** (`model`, `quoter`,
   `execution`, `portfolio`, `risk`, `exchanges`, `market_handler`).
   Use only standard library and modules internal to `alpha` in P2.
   Put adapters for the existing MM quoter under `quoter/`, not here.
2. **Decimals** for every financial numerical value, with finite-value
   validation and deterministic precision. Keep observed data immutable.
3. **Separated responsibilities:** `PriceFeatureCalculator` is pure;
   `FeatureEngine` owns bounded rolling data, availability and quality;
   `SignalEngine` owns forecast model dispatch/validation;
   `Strategy` owns desired trading *policy decisions*, not orders.
4. **Replay = live ingestion contract:** both sources deliver normalized
   `PriceBar` to `FeatureEngine.on_bar(bar, at_ms=...)`, or to the
   composed `AlphaPipeline.on_bar`. Do not introduce a strategy-visible
   `live` flag or a Redis lookup in the hot path.
5. **Provenance:** maintain symbol, observation/availability time,
   feature/model version, signal lineage and decision horizon. Unknown,
   stale, contradictory or out-of-order input fails closed. Gaps force
   bounded rewarm; no silent backfill.
6. **Strategies share one interface:** `Strategy.decide(StrategyContext)`
   returns `StrategyDecision` with `TargetExposure` or `QuoteProposal`.
   The existing P1 `AlphaStrategy.generate_signal()` is a legacy predictor
   interface; use `SignalModel.predict()` for new forecasts.
7. **Never trade from this package.** Risk authorization, venue precision,
   reconciliation, order/position management and authenticated sends are
   independent downstream responsibilities.
8. Update `notes/alpha_engine/IMPLEMENTATION_LOG.md`,
   `DESIGN_DECISIONS.md` and `HANDOFF.md` with every significant
   implementation change, including actual CI evidence and unresolved
   market-data/corporate-action assumptions.
9. Early algorithms are reference baselines, not statistically validated
   alphas. No claim of profitability from deterministic fixture tests.

Before moving this package to its own repository, add pinned packaging and
release metadata, serialization/ABI contracts, separate dependency/CI
configuration, and tests against both importing applications.
