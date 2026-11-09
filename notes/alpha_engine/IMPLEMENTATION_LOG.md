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
| P2-A PR | **Open draft:** [PR #16](https://github.com/kyle-nguyennn/market-maker/pull/16) |
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
- [x] P2-A code validated at commit `7245a401df2c42947506bf04687dc005615d3e00`: [CI run #42576909832](https://github.com/kyle-nguyennn/market-maker/actions/runs/42576909832), **324 passed** (default), **324 passed** (`PERPETUAL_ALPHA_DEX=pas`), read-only PAS job passed. Earlier run #42576707495 identified a test-only ambient Decimal-context assertion; corrected before this green run.
- [ ] Review [PR #16](https://github.com/kyle-nguyennn/market-maker/pull/16) and verify latest HEAD checks before merge. No real market-data source or live API calls yet.
- [ ] P2-B: trading-calendar-aware equity/session preprocessing, vendor-specific adjustment/revision policies and baseline fixed strategy.
- [ ] P2: trend, momentum, mean-reversion strategies and static ensemble baseline with identical signal contract.
- [ ] P2: regime classifier / selector with a fixed-baseline comparison; avoid hidden fitting on the test period.
- [ ] P3: walk-forward evaluator with slippage, fees, latency, turnover, leakage control and confidence/stability reporting.
- [ ] P4: portfolio construction + target-to-position trade planner, risk constraints and durable reconciliation (separate from existing resting-order manager).
- [ ] P5: small AlphArena sandbox test and then expressly approved forward paper test.
- [ ] P6: SHIFT directional and market-maker quote skew translation, event/horizon recalibration and order-book fill testing.

## P2-A code inventory (on active branch)

- `alpha/observations.py` — portable immutable `PriceBar` and source-port `BarProvider`; no file I/O.
- `market_handler/historical_bar_reader.py` — host-owned strict `read_price_bars_csv` returning `PriceBar`. (`alpha/market_data.py` was removed.)
- `alpha/features.py` — `FeatureConfig`, `build_price_features` with as-of filtering, gap/stale/revision rejection and deterministic Decimal feature values.
- `alpha/__init__.py` — public exports, with P1 contracts retained.
- `tests/test_alpha_features.py` — deterministic fixtures for future/late data, duplicates, gaps, stale data, precision, CSV and immutability.
- `notes/alpha_engine/DESIGN_DECISIONS.md` — ADR-006 through ADR-009.

**P2-A limitations:** no vendor feed or stock market calendar; strict UTC bar continuity is unsuitable across market closures. Features are observations, not predictive strategies; no trading permission, portfolio sizing or backtest P&L. See ADR-007 and handoff.

## P2-A architecture refactor — shared engines and Strategy families (same PR #16)

- [x] Extract pure `FeatureCalculator` Protocol and `PriceFeatureCalculator` reference class; `build_price_features` delegates calculation.
- [x] Add bounded rolling `FeatureEngine.on_bar(bar, at_ms=...)` with time/source/sequence gates, warmup and gap reset.
- [x] Add independent `SignalEngine.on_features(snapshot, generated_at_ms=...)` and clock-injected `SignalModel.predict` with signal provenance checks. Preserve legacy P1 `AlphaStrategy` protocol.
- [x] Add `StrategyContext` (market/features/signals/portfolio), `Strategy.decide` and `StrategyDecision` with explicit exposure or quote proposal output.
- [x] Add `MomentumStrategy` and `MeanReversionStrategy` as **fixed threshold policy skeletons**. No fitted alpha predictor or parameter selection yet.
- [x] Add `quoter/alpha_strategy_adapter.py`: wrap legacy `BaseMMQuoter` or future context-aware quoter without importing market-maker modules from `alpha/`.
- [x] Add `AlphaPipeline.on_bar` and `replay_pipeline`; historical and live-style bar inputs use the same engine and strategy method.
- [x] Extend `tests/test_alpha_engines.py` with replay/live equivalence, schema/source isolation, out-of-order and gap recovery, immutable portfolio, signal lineage, directional family and MM adapter tests.
- [x] Capture extraction boundary and engine/policy decisions in ADR-010..013 and handoff.
- [x] **CI evidence (pre-notes-update):** [run #42579976084](https://github.com/kyle-nguyennn/market-maker/actions/runs/42579976084) at code commit `1a6159e987396d3794e69a71cfd76e092c36aff4`, **338 passed** in default and PAS configurations. Read-only PAS job passed; no authenticated AlphArena operations.
- [ ] Verify CI for latest documentation HEAD before P2-A merge; leave PR #16 draft pending review.

**Boundary note:** `alpha/` is temporarily vendored into `market-maker`, intentionally standard-library-only so it can be extracted later. `quoter/alpha_strategy_adapter.py` remains in `market-maker` and is not part of the future library. `SignalEngine` currently runs model objects provided by the caller; test fixtures are not validated trading models. Redis/feature store, live WS feed aggregation, session calendars, persistence and production risk/dispatch are not implemented.

## P2-A market-data / alpha boundary cleanup (same PR #16)

- [x] Remove `alpha/market_data.py`, split normalized bar contracts into `alpha/observations.py`, and move CSV parsing/I/O to host `market_handler/historical_bar_reader.py`.
- [x] Update calculators, feature builder/engine, pipeline, public exports and tests to consume `alpha.observations.PriceBar`; remove host reader from published `alpha.__all__`.
- [x] Keep one ingestion contract for replay and live: `FeatureEngine.on_bar(PriceBar, at_ms=...)` / `AlphaPipeline.on_bar`.
- [x] Add `tests/test_market_data_boundary.py` for CSV→normalized bar→FeatureEngine parity, independent replay vs live-style ingestion, and package import separation.
- [x] Confirm AlpArena is a **paper trade/portfolio API** as documented at [alpharena.io/docs](https://alpharena.io/docs). The current public API does not document an OHLCV/bar feed; use a separate equities data source in the host layer rather than inventing `AlphArenaMarketDataHandler`.
- [x] Initial boundary-code run [#42591043583](https://github.com/kyle-nguyennn/market-maker/actions/runs/42591043583): **338 passed** under default and PAS modes before adding the boundary-specific tests.
- [x] Boundary/refactor latest-code CI: [run #42591619167](https://github.com/kyle-nguyennn/market-maker/actions/runs/42591619167) at `2074558bec3fef057428488852f7b8b255dad6f1`, **341 passed** default and **341 passed** with `PERPETUAL_ALPHA_DEX=pas`. PAS read-only succeeded; live-testnet read-only skipped. No trading API calls.
- [ ] Review and merge PR #16 separately when requested; documentation-only commits require their own latest-head CI confirmation.

**Module ownership:** venue/live readers and historical CSV/Parquet providers belong to host `market_handler/` or source-specific adapters; only normalized observation types, FeatureEngine, SignalEngine and Strategy belong inside portable `alpha/`. The existing `market_handler/hyperliquid_market_data_handler.py` is a *different* L2 source adapter and has not been reused for AlpArena.

## P2-A Alpaca SDK host market-data integration (same PR #16)

- [x] Installable optional pinned SDK: `requirements/alpaca.txt` → `alpaca-py==0.45.0` (not a dependency of `alpha/` or the existing Hyperliquid runtime).
- [x] `market_handler/alpaca_stock_market_data.py`: `AlpacaHistoricalBarProvider` accepts an explicitly configured start, source version, IEX/SIP feed, RAW/corporate-action adjustment and **assumed** publication lag; fetches through `StockHistoricalDataClient.get_stock_bars` into `PriceBar`. Query bounds exclude incomplete/unavailable bars; bar start is shifted to completed end.
- [x] `AlpacaMinuteBarStream` wraps SDK `StockDataStream.subscribe_bars` and records actual callback-receipt UTC milliseconds. It invokes a caller-supplied async `on_bar(PriceBar, at_ms)` and requires explicit `subscribe()`/`run()`. No SDK TradingClient or order submission.
- [x] Strict no-network tests in `tests/test_alpaca_market_data.py`; a dedicated `.github/workflows/alpaca-market-data-tests.yml` installs the **real SDK** for request model validation.
- [x] `market_handler/ALPACA.md` documents credentials, read-only examples, historical availability assumption, float precision loss, corrections/rewarm limits and no AlpArena fake market-data endpoint.
- [x] ADR-015 captures integration boundary and research caveats.
- [x] **Installed SDK compatibility:** [initial run #42595183411](https://github.com/kyle-nguyennn/market-maker/actions/runs/42595183411) caught `Sort` imported from the wrong SDK package; subsequent runs caught value-based TimeFrame comparison and normalized naive-UTC request datetime semantics. Corrected adapter and smoke test.
- [x] **Last verified pre-stream-smoke run:** [Alpaca SDK #42595645025](https://github.com/kyle-nguyennn/market-maker/actions/runs/42595645025), **16 passed**, SDK 0.45 installed, zero API calls. [Full Hyperliquid contract run #42595645012](https://github.com/kyle-nguyennn/market-maker/actions/runs/42595645012), **353 passed, 1 skipped** in both default and PAS selections; PAS read-only job succeeded. A subsequent change added a real-SDK live constructor/registration smoke test; check newest CI before merging.
- [x] **Final verified implementation CI:** [real Alpaca SDK #42595956921](https://github.com/kyle-nguyennn/market-maker/actions/runs/42595956921), **17 passed** (including real-SDK request model and stream constructor/registration, no network). [full project #42595957138](https://github.com/kyle-nguyennn/market-maker/actions/runs/42595957138), **353 passed, 2 skipped** in default and PAS configurations; read-only PAS succeeded. Skips correspond to optional Alpaca SDK smoke tests in the core-only environment. No authenticated Alpaca requests or live sockets were started.
- [ ] Reconfirm latest-head checks after final documentation update; review PR #16 before merging.
- [ ] Before production usage: obtain market-data access, collect a small historical fixture and measure actual time/availability, model sessions and revisions, then integrate scheduling/reconnect/backpressure and separate risk gates.

## Test command

```bash
python -m pytest -q tests/test_alpha_domain.py tests/test_alpha_features.py tests/test_alpha_engines.py tests/test_market_data_boundary.py tests/test_alpaca_market_data.py
python -m pytest -q
PERPETUAL_ALPHA_DEX=pas python -m pytest -q
```

P1/P2-A tests are offline. P2-A CI evidence must be recorded only after observed, not inferred from the source.
