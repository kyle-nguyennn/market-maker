# Alpha engine — implementation log

**Updated:** 2026-11-09 (Asia/Singapore). **Repository:** `kyle-nguyennn/market-maker`.

## Baselines

| Item | Status / link |
| --- | --- |
| AlphArena API HTTP boundary | **Merged** — [PR #13](https://github.com/kyle-nguyennn/market-maker/pull/13) |
| Initial architectural intent | `exchanges/alpharena/NOTES.md` on main |
| P1 development branch | `feat/shared-alpha-domain-p1` |
| P1 PR | **Merged** — [PR #15](https://github.com/kyle-nguyennn/market-maker/pull/15), squash commit `7d4c774d4ebdb24ebe0e081bb99b1b072a5ea288` |
| P2-A branch | `feat/alpha-p2-point-in-time-features` (based on post-P1 `main`) |
| P2-A PR | To be recorded after opening |
| Live AlphArena sandbox trade test | **Not performed / not authorized** |

## P1 — Domain interfaces (this branch)

- [x] `alpha/domain.py`: `FeatureSnapshot` with observation/availability timestamps, lookback, feature source+version, immutable Decimal feature mapping.
- [x] `alpha/domain.py`: `AlphaSignal` with signed return, confidence, model/strategy lineage and forecast horizon.
- [x] `alpha/domain.py`: `TargetExposure` with signed equity weight, decision/signal lineage, horizon and confidence.
- [x] `alpha/strategy.py`: venue-neutral `AlphaStrategy.generate_signal()` Protocol supporting abstention.
- [x] `tests/test_alpha_domain.py`: deterministic valid/invalid, precision, immutability, abstention and no-order-contract cases.
- [x] Document decisions and next-agent handoff under `notes/alpha_engine/`.
- [x] Initial P1 code validated at commit `88e0892c0aecc0765a5dc9c268e639a2bc29cebc`: [GitHub Actions run #42574503050](https://github.com/kyle-nguyennn/market-maker/actions/runs/42574503050), **287 passed** (default), **287 passed** (`PERPETUAL_ALPHA_DEX=pas`); Python syntax and read-only CI jobs passed. Subsequent documentation-only commits should get a fresh CI status before merge.
- [x] PR #15 merged into `main` after passing 287 tests in both CI configurations.

**Explicit P1 non-goals:** AlpArena REST changes, venue execution, risk policy, realized P&L, strategy computation, alpha model training, live/paper position submission.

## Next milestones (no claim of implementation)

- [x] P2-A implementation on development branch: versioned immutable OHLCV `PriceBar` with observation and availability clocks, strict Decimal CSV loader, injected provider Protocol, point-in-time feature builder, configuration, SHA-256 snapshot identity and adversarial tests.
- [ ] P2-A CI validation on latest HEAD, review and merge. No real market-data source or live API calls yet.
- [ ] P2-B: trading-calendar-aware equity/session preprocessing, vendor-specific adjustment/revision policies and baseline fixed strategy.
- [ ] P2: trend, momentum, mean-reversion strategies and static ensemble baseline with identical signal contract.
- [ ] P2: regime classifier / selector with a fixed-baseline comparison; avoid hidden fitting on the test period.
- [ ] P3: walk-forward evaluator with slippage, fees, latency, turnover, leakage control and confidence/stability reporting.
- [ ] P4: portfolio construction + target-to-position trade planner, risk constraints and durable reconciliation (separate from existing resting-order manager).
- [ ] P5: small AlphArena sandbox test and then expressly approved forward paper test.
- [ ] P6: SHIFT directional and market-maker quote skew translation, event/horizon recalibration and order-book fill testing.

## P2-A code inventory (on active branch)

- `alpha/market_data.py` — `PriceBar`, `BarProvider`, strict `read_price_bars_csv`.
- `alpha/features.py` — `FeatureConfig`, `build_price_features` with as-of filtering, gap/stale/revision rejection and deterministic Decimal feature values.
- `alpha/__init__.py` — public exports, with P1 contracts retained.
- `tests/test_alpha_features.py` — deterministic fixtures for future/late data, duplicates, gaps, stale data, precision, CSV and immutability.
- `notes/alpha_engine/DESIGN_DECISIONS.md` — ADR-006 through ADR-009.

**P2-A limitations:** no vendor feed or stock market calendar; strict UTC bar continuity is unsuitable across market closures. Features are observations, not predictive strategies; no trading permission, portfolio sizing or backtest P&L. See ADR-007 and handoff.

## Test command

```bash
python -m pytest -q tests/test_alpha_domain.py tests/test_alpha_features.py
python -m pytest -q
PERPETUAL_ALPHA_DEX=pas python -m pytest -q
```

P1/P2-A tests are offline. P2-A CI evidence must be recorded only after observed, not inferred from the source.
