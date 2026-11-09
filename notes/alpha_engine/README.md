# Shared alpha engine — implementation notes

**Scope:** venue-independent alpha research and target-exposure interface used by offline replay, AlphArena forward paper testing and HFTC/SHIFT execution. The repository's pre-existing resting-order market maker is preserved.

- [IMPLEMENTATION_LOG.md](./IMPLEMENTATION_LOG.md) — milestone checklist, verified progress, test and PR status
- [DESIGN_DECISIONS.md](./DESIGN_DECISIONS.md) — architecture decision log (ADRs) with alternatives / implications
- [HANDOFF.md](./HANDOFF.md) — next-agent start instructions and immediate work queue
- [AlphArena pilot architecture](../../exchanges/alpharena/NOTES.md) — original objectives, experiment design and venue caveats

**Implementation baseline:** merged [PR #13](https://github.com/kyle-nguyennn/market-maker/pull/13) provides AlphArena's HTTP paper-position boundary. P1 [PR #15](https://github.com/kyle-nguyennn/market-maker/pull/15) and P2 [PR #16](https://github.com/kyle-nguyennn/market-maker/pull/16) are merged into `main`. P2-C baseline StrategyEngine [PR #17](https://github.com/kyle-nguyennn/market-maker/pull/17) is merged. The active offline P3 causal bar backtester is draft [PR #18](https://github.com/kyle-nguyennn/market-maker/pull/18), branch `feat/alpha-p3-causal-backtest`. Notes must be updated with actual code/CI evidence and unresolved decisions after each significant change.

**Current package boundary:** `alpha/` is **vendored into market-maker** for iteration, but maintained as a standard-library-only package with its own [AGENTS.md](../../alpha/AGENTS.md) and extractable public interface. `PriceFeatureCalculator` is pure; `FeatureEngine` handles rolling market observations; `SignalEngine` validates predictions; `Strategy.decide(StrategyContext)` unifies directional targets and market-making quotes via an external adapter. Both live and replay sources call the same `on_bar` API. `alpha/observations.py` owns the immutable `PriceBar` contract; the CSV reader lives in `market_handler/historical_bar_reader.py`. See ADR-010..018, the [strategy guide](../../alpha/STRATEGIES.md), [backtest guide](../../backtest/README.md), and the handoff.

**Research objective:** Compare regime-aware selection *after costs* with best fixed policy and static equal-weight ensemble using strictly walk-forward data. AlphArena alone cannot validate spread capture, latency, queue positioning or fill probabilities; SHIFT and offline order-book replay must measure those separately.

## Invariants

1. `model/AGENTS.md` governs venue neutrality and `Decimal` numerics. No exchange SDK or HTTP calls inside `alpha/`.
2. Research outputs are decisions, **not orders or authority to trade**. Portfolio policy and execution risk own sizing constraints and dispatch.
3. Only features available at decision time may enter a signal. Preserve original observation timestamp, availability timestamp, horizon, source/version and lineage IDs.
4. No API calls or trade submissions from unit tests. All forward trading requires separately authorized credentials and safety controls.
5. Keep future features, strategies, ensemble/selection, walk-forward evaluator and venues in separate incremental PRs with reproducible tests.
