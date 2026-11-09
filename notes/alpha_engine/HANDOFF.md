# Next-agent handoff — shared alpha engine

**Date:** 2026-11-09; **active P1 branch:** `feat/shared-alpha-domain-p1`; **base:** `main`.

## First five minutes

1. Read [README](./README.md), [DESIGN_DECISIONS](./DESIGN_DECISIONS.md), [IMPLEMENTATION_LOG](./IMPLEMENTATION_LOG.md), and `exchanges/alpharena/NOTES.md`.
2. Inspect `model/AGENTS.md`, `alpha/domain.py`, `alpha/strategy.py`, `tests/test_alpha_domain.py` and `quoter/base_quoter.py`.
3. Check the latest P1 PR status and head SHA, then run offline alpha tests and the full repository suite. Do not assume earlier passes cover later commits.
4. Preserve immutable Decimal domain invariants and existing market-making functionality.
5. Update log and ADRs with actual changes, test runs, blocked assumptions and link to the next PR.

## P1 acceptance

- All domain types import without AlphArena/SHIFT/SDK/HTTP dependencies.
- Every continuous feature and trading input is finite Decimal, preserving tiny values.
- A feature is not usable before its availability timestamp; a forecast cannot predate input feature availability.
- Distinguish a signed expected return from a signed equity weight; no venue weights/caps checked in domain layer.
- Feature snapshots defensively copy data; no mutation of recorded inputs.
- Strategies are pure and may abstain. No invocation of SDKs, orders, or live sandbox requests.
- Existing test suites pass under normal and `PERPETUAL_ALPHA_DEX=pas` configurations.

## Recommended P2 starting slice (future PR only)

Implement a **reproducible feature construction adapter for offline price bars** with an injected clock/data source, explicit as-of timestamp, latency/availability, missing-data handling, model/feature versioning and a no-lookahead fixture. Then implement a simple fixed baseline trend strategy that emits `AlphaSignal`. Do not start from a leaderboard-driven RL optimizer or a live bot. Add walks and cost modeling in distinct reviewed increments.

## Known caveats

- `TargetExposure` does **not** ensure actual capacity or access to trades; downstream risk/portfolio layers must enforce policy.
- The `AlphaStrategy` Protocol is structural, not a runtime guarantee of purity or lineage consistency.
- Converting a TargetExposure to an AlphArena trade can require opening/closing *discrete positions*, not exact continuous rebalancing.
- `FeatureSnapshot.values` uses read-only mapping proxy; serializers must implement a deliberate Decimal-preserving codec.
- SHIFT needs event-level and order-book data to test 15-tick signals, liquidity and queue effects; AlphArena cannot supply that evidence.

**Safety:** Do not dispatch or sign real/simulated trading operations without explicit authorization. CI may run read-only tests but must not be used to submit trades.
