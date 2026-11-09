# Alpha engine — implementation log

**Updated:** 2026-11-09 (Asia/Singapore). **Repository:** `kyle-nguyennn/market-maker`.

## Baselines

| Item | Status / link |
| --- | --- |
| AlphArena API HTTP boundary | **Merged** — [PR #13](https://github.com/kyle-nguyennn/market-maker/pull/13) |
| Initial architectural intent | `exchanges/alpharena/NOTES.md` on main |
| P1 development branch | `feat/shared-alpha-domain-p1` |
| P1 PR | Pending until opened; update when available |
| Live AlphArena sandbox trade test | **Not performed / not authorized** |

## P1 — Domain interfaces (this branch)

- [x] `alpha/domain.py`: `FeatureSnapshot` with observation/availability timestamps, lookback, feature source+version, immutable Decimal feature mapping.
- [x] `alpha/domain.py`: `AlphaSignal` with signed return, confidence, model/strategy lineage and forecast horizon.
- [x] `alpha/domain.py`: `TargetExposure` with signed equity weight, decision/signal lineage, horizon and confidence.
- [x] `alpha/strategy.py`: venue-neutral `AlphaStrategy.generate_signal()` Protocol supporting abstention.
- [x] `tests/test_alpha_domain.py`: deterministic valid/invalid, precision, immutability, abstention and no-order-contract cases.
- [x] Document decisions and next-agent handoff under `notes/alpha_engine/`.
- [ ] Confirm full suite in CI for P1 HEAD; record exact run link, test counts and failures here.
- [ ] Review P1 PR and merge after checks, if requested.

**Explicit P1 non-goals:** AlpArena REST changes, venue execution, risk policy, realized P&L, strategy computation, alpha model training, live/paper position submission.

## Next milestones (no claim of implementation)

- [ ] P2: historical/live market data abstraction and reproducible `FeatureSnapshot` construction (explicit availability and quality).
- [ ] P2: trend, momentum, mean-reversion strategies and static ensemble baseline with identical signal contract.
- [ ] P2: regime classifier / selector with a fixed-baseline comparison; avoid hidden fitting on the test period.
- [ ] P3: walk-forward evaluator with slippage, fees, latency, turnover, leakage control and confidence/stability reporting.
- [ ] P4: portfolio construction + target-to-position trade planner, risk constraints and durable reconciliation (separate from existing resting-order manager).
- [ ] P5: small AlphArena sandbox test and then expressly approved forward paper test.
- [ ] P6: SHIFT directional and market-maker quote skew translation, event/horizon recalibration and order-book fill testing.

## Test command

```bash
python -m pytest -q tests/test_alpha_domain.py
python -m pytest -q
PERPETUAL_ALPHA_DEX=pas python -m pytest -q
```

All P1 tests are designed to be offline. CI evidence should be recorded only after observed, not inferred from the test source.
