# Shared alpha engine — implementation notes

**Scope:** venue-independent alpha research and target-exposure interface used by offline replay, AlphArena forward paper testing and HFTC/SHIFT execution. The repository's pre-existing resting-order market maker is preserved.

- [IMPLEMENTATION_LOG.md](./IMPLEMENTATION_LOG.md) — milestone checklist, verified progress, test and PR status
- [DESIGN_DECISIONS.md](./DESIGN_DECISIONS.md) — architecture decision log (ADRs) with alternatives / implications
- [HANDOFF.md](./HANDOFF.md) — next-agent start instructions and immediate work queue
- [AlphArena pilot architecture](../../exchanges/alpharena/NOTES.md) — original objectives, experiment design and venue caveats

**Implementation baseline:** merged [PR #13](https://github.com/kyle-nguyennn/market-maker/pull/13) provides only AlphArena's HTTP boundary. P1 resides in `feat/shared-alpha-domain-p1` until reviewed and merged. These notes MUST be updated by each agent after code changes or decisions, including what was actually tested.

**Research objective:** Compare regime-aware selection *after costs* with best fixed policy and static equal-weight ensemble using strictly walk-forward data. AlpArena alone cannot validate spread capture, latency, queue positioning or fill probabilities; SHIFT and offline order-book replay must measure those separately.

## Invariants

1. `model/AGENTS.md` governs venue neutrality and `Decimal` numerics. No exchange SDK or HTTP calls inside `alpha/`.
2. Research outputs are decisions, **not orders or authority to trade**. Portfolio policy and execution risk own sizing constraints and dispatch.
3. Only features available at decision time may enter a signal. Preserve original observation timestamp, availability timestamp, horizon, source/version and lineage IDs.
4. No API calls or trade submissions from unit tests. All forward trading requires separately authorized credentials and safety controls.
5. Keep future features, strategies, ensemble/selection, walk-forward evaluator and venues in separate incremental PRs with reproducible tests.
