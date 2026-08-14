# Market-Making Implementation Plan

Consolidates the Akuna implementation brief (formerly this file), `docs/MARKET_MAKING_AGENTS.md`, and the current code skeleton into one actionable plan.

**Goal:** a small, modular market-making research harness where fair value, spread, skew, and sizing are swappable components; Optiver Ready Trader Go (RTG) is the first live venue behind an adapter; the Akuna 2026 challenge plugs in later as a second adapter. Optimize for correctness, reproducibility, observability, and fast experimentation — not sophistication.

Core loop: **estimate value → choose quotes → receive fills → update beliefs & inventory → re-quote.**

---

## 1. Current State

### What exists

| Component | File | Status |
|---|---|---|
| Market state / adapter | `market_handler/market_adapter.py` | Empty stub — `MarketState` is `pass` |
| Quoter interfaces | `quoter/base_quoter.py` | `Side`, `Quote`, `BaseQuoter`, `BaseMMQuoter` with the fair → reservation → spread → size pipeline |
| RTG venue | `exchanges/Optiver-Ready-Trader-Go/` | Vendored simulator + `TraderOne.py` (working but monolithic reference trader) |
| Docs | `docs/` | This plan; `MARKET_MAKING_AGENTS.md` (architecture conventions, AS model notation, calibration protocol) |

No packaging (`pyproject.toml`), no tests, no config system, no simulator-independent runner yet.

### Skeleton observations to address

1. **Sign bug in `BaseMMQuoter.quote`** (`quoter/base_quoter.py:49`): `ask_price = reservation - ask_offset`. Either the ask must be `reservation + ask_offset`, or the convention is "signed offsets" — pick one, document it, and add a unit test that asserts `bid < reservation < ask` for positive offsets.
2. **`MarketState` is empty.** It is the contract between adapters and quoters; defining it is the first real task (§4).
3. **Prices are `int`** in `Quote` — this matches the AGENTS.md convention (integer ticks/cents at boundaries) and RTG's cent-denominated prices. Keep it; convert float model prices to ticks in exactly one tested boundary function.
4. **`TraderOne.py` mixes everything** (strategy, order tracking, position) in exchange callbacks. It stays as a reference; new work goes through the adapter/quoter split and never embeds venue rules in strategy code.

### RTG venue facts (drive adapter + risk design)

From `exchanges/Optiver-Ready-Trader-Go/exchange.json` and `ready_trader_go/base_auto_trader.py`:

- Two instruments: `FUTURE` (hedge venue) and `ETF` (quoted instrument); ETF clamped to ±0.2% of future.
- Prices are integer cents; tick size 100 cents; top-5 book levels via `on_order_book_update_message`; trade activity via `on_trade_ticks_message`.
- Fees: maker −0.01% (rebate), taker +0.02%.
- Hard limits: position ±100, ≤10 active orders, ≤200 active volume, ≤50 messages/sec (breach = disqualification).
- Order lifecycle callbacks: `on_order_filled_message` (per-fill price/volume), `on_order_status_message` (fill/remaining/fees; remaining 0 = done/cancelled), `on_error_message`, hedge fills.
- Matches are deterministic given a data file; replay supported (`rtg.py run|replay`).

---

## 2. Target Architecture

Keep the existing top-level package names and grow into this layout (adapted from the brief's proposal + AGENTS.md layering; dependencies point inward — nothing in `quoter/`, `portfolio/`, or `analytics/` may import venue code):

```text
market-maker/
├── market_handler/          # normalized market state + venue adapters
│   ├── market_adapter.py    #   MarketState, BaseMarketAdapter
│   └── rtg_adapter.py       #   RTG BaseAutoTrader ↔ domain events
├── quoter/                  # pure strategy logic (no I/O, no order sending)
│   ├── base_quoter.py       #   existing interfaces (fix ask sign)
│   ├── components.py        #   fair value / reservation / spread / size models
│   └── strategies.py        #   Strategy 0–4 as BaseMMQuoter compositions
├── execution/               # order manager: desired quotes → venue actions
│   └── order_manager.py     #   reconcile, tick rounding, ID/ack/partial-fill tracking
├── risk/                    # independent of strategy; can veto/clamp intents
│   └── risk_manager.py      #   limits, staleness, message budget, kill switch
├── portfolio/               # cash, inventory, fees, realized/marked PnL
│   └── accounting.py
├── sim/                     # toy simulator (pre-Akuna training environment)
│   └── toy_sim.py
├── analytics/               # markouts, PnL decomposition, run metrics
├── experiments/             # runner, seed sweeps, results storage (CSV/Parquet)
├── config/                  # yaml/json parameter files; no magic numbers in code
├── exchanges/               # vendored venues (RTG now, Akuna later) — untouched
└── tests/
```

Event-processing order (from AGENTS.md, applies to sim, RTG, and Akuna alike):

1. Normalize event → update market/order/portfolio state.
2. Recompute risk status (process fills even when quoting is disabled).
3. If quoting allowed and market fresh/valid: ask quoter for desired quotes.
4. Risk-filter → order-manager reconcile → send → log the full decision record.

---

## 3. Division of Responsibilities

- **`market_handler`** — translate venue messages into `MarketState` snapshots and normalized fill/order/error events. Detect stale data, crossed/missing book, sequence gaps. No strategy math.
- **`quoter`** — pure functions of `MarketState` + portfolio + config → desired `Quote`s. Never sends/cancels orders (already stated in the skeleton docstring — keep it true).
- **`execution`** — diff desired quotes against live/pending orders; apply side-aware tick rounding, no-cross checks, duplicate suppression, cancel/replace policy; idempotent under duplicate callbacks.
- **`risk`** — hard inventory/size/volume/message-rate/loss limits from config; kill switch cancels everything and blocks new orders; never adapted away by strategy.
- **`portfolio`** — exact fill reconciliation: cash, signed inventory `q`, fees, realized + marked PnL (`W = cash + q·V`), documented mark and sign convention.
- **`analytics`** — offline: markouts at 1/5/10 steps per fill, PnL decomposition (spread capture + inventory MTM + fees + residual ≈ 0), inventory/execution metrics.

---

## 4. Core Data Model

Flesh out `MarketState` (merging the brief's minimal dataclass with AGENTS.md required state and RTG's top-5 book):

```python
@dataclass(frozen=True)
class MarketState:
    instrument: int
    timestamp: float          # monotonic sim/exchange time
    sequence: int
    bid_prices: list[int]     # ticks (cents); index 0 = best
    bid_volumes: list[int]
    ask_prices: list[int]
    ask_volumes: list[int]
    last_trade_price: int | None
    last_trade_side: Side | None
    time_remaining: float | None
    is_valid: bool            # not stale/crossed/missing-side
    # derived helpers: best_bid, best_ask, mid, microprice, spread_ticks
```

Supporting types: `Fill(timestamp, side, price, qty, fee, fair_value_at_fill)`, `Portfolio(cash, inventory, fees_paid, realized_pnl)`, `OrderIntent` / live-order records for the order manager. Fields get revised when the Akuna API lands — adapters absorb the change, not the quoters.

Units convention (non-negotiable, from AGENTS.md): integer ticks at all exchange boundaries; explicit units on `sigma` (price per √time), `k` (1/price); monotonic time for ordering; never compare float prices for equality.

---

## 5. Strategy Ladder

Each strategy is a `BaseMMQuoter` subclass composed from swappable components; every earlier stage stays runnable as a control. Quotes: `bid = r − s/2`, `ask = r + s/2` with `r = fair − skew`.

| # | Strategy | fair | reservation | spread | Purpose / research question |
|---|---|---|---|---|---|
| 0 | Fixed quotes | reference value | = fair | constant | Validate simulator + PnL accounting |
| 1 | Midpoint MM | `(B+A)/2` | = fair | constant | Simplest realistic baseline |
| 2 | Inventory-aware | midpoint | `fair − k_inv·q` | constant | Sweep `k_inv`: PnL sacrificed vs inventory risk reduced |
| 3 | Volatility-aware | midpoint | `fair − k_inv·q` | `base + α·σ_realized` | Demand more edge in dangerous states |
| 4 | Signal-aware | microprice or `mid + α·flow` | as above | as above | Validate signals across seeds, out-of-sample |
| 5 | Fixed-`k` Avellaneda–Stoikov | documented fair | `s − q·γ·σ²·τ` | `γσ²τ + (2/γ)·ln(1+γ/k)` | `A`,`k` from offline calibration artifacts only — never a hand-tuned live knob |

AGENTS.md constraints on stage 5: keep a fixed-`k` control before any adaptive estimation; calibrate `λ(δ) = A·e^(−kδ)` from quote exposure + fills with censoring (non-fills and time-at-risk count); chronological train/validation/test splits; versioned calibration artifacts. Adaptive variants come later, config-gated, compared against the unchanged fixed baseline (see "Gated adaptation" in `MARKET_MAKING_AGENTS.md`).

---

## 6. Milestones

Target from the brief: **a usable research harness by Aug 17** (three days out), not an advanced market maker.

### M0 — Contracts & scaffolding (first)
- `pyproject.toml`, pytest, formatter/linter; document run commands in README.
- Define `MarketState`, `Fill`, `Portfolio`, `OrderIntent`; fix the `BaseMMQuoter` ask-price sign and add tests for the quote pipeline (bid < ask, correct skew direction for long/short/flat).
- Config loading (dataclass or yaml) with validated parameters and seeds.

### M1 — Toy simulator + Strategies 0–2
- `sim/toy_sim.py`: seeded random-walk price (`S_{t+1} = S_t + ε`, `ε ∈ {−1,0,+1}` in ticks), configurable customer arrival flow, quote submission, touch-based fills, position limits, terminal marking, event log. Batch-runnable.
- `portfolio/accounting.py` with unit tests from known fill sequences (incorrect PnL invalidates every later experiment).
- Run Strategies 0, 1, 2 end-to-end; record fills with `fair_value_at_fill`.

### M2 — Analytics & experiments
- Markouts (1/5/10 steps, per side), PnL decomposition, inventory metrics (`mean/max |q|`, variance, terminal `q`, limit hits), execution metrics (fill rate, edge at fill).
- `experiments/runner.py`: one config → N seeds → metrics table; `sweeps.py` over `base_spread × inventory_penalty` (e.g. spreads `[0.5,1,2,3,5]`, penalties `[0,0.05,0.1,0.2,0.5]`); persist results as CSV/Parquet. Never conclude from one path.
- Regime stress: high vol, zero vol, one-sided flow, price jumps.

### M3 — RTG adapter (first real venue)
- `market_handler/rtg_adapter.py`: subclass `BaseAutoTrader`; translate book/tick/fill/status/error callbacks into domain events; expose `MarketState` snapshots for the ETF; forward validated `OrderIntent`s via `send_insert_order`/`send_cancel_order`.
- `execution/order_manager.py`: quote reconciliation like `TraderOne.py` does inline (cancel-if-price-moved, one working order per side initially), plus partial-fill and ack tracking, idempotency under duplicate callbacks.
- `risk/risk_manager.py` sized to RTG: position ±100 with headroom, ≤10 active orders, ≤200 active volume, message budget under 50/s, stale-data cancel, kill switch. Enforced outside the quoter.
- Hedging policy for the FUTURE leg (RTG penalizes unhedged lots) lives in execution/risk, not in the quoter.
- Run Strategy 1–2 in an RTG match; reconcile our accounting against `score_board.csv` / `match_events.csv`.

### M4 — Strategies 3–5 + calibration
- Realized-volatility estimator (incremental, bounded window) and vol-aware spread.
- Microprice / order-flow signal components; measure predictiveness before trusting (`E[next_return | ask_fill]` etc.).
- Fill-intensity data collection (exposure intervals, δ, fills, censoring) from sim + RTG runs; fit fixed `A`,`k`; add AS quoter variant; compare out-of-sample against Strategies 1–2.

### M5 — Akuna adapter (when rules release)
- Answer the brief's §14 question list first (scoring, observable state, actions, fills, order management, limits, fees, horizon, participants, multi-instrument).
- Write a compatibility-gap analysis before touching core interfaces; implement `market_handler/akuna_adapter.py` + competition config; recalibrate — carry no RTG parameters across untested.

---

## 7. Testing Requirements (condensed)

- **Unit:** reservation sign/magnitude for long/flat/short; spread formula edge cases; side-aware tick rounding + non-crossing; PnL from known fill sequences; order state transitions (duplicates, partials, rejects, post-cancel fills); config validation; calibration recovery of known `A`,`k` from synthetic data.
- **Integration:** market-data → quote → order lifecycle through adapter boundaries; fill propagation to cash/inventory/risk/logs; kill switch actually cancels and blocks; sim and RTG adapters pass the same contract tests.
- **Property/simulation:** same input+config+seed ⇒ identical output; hard limits never breached under randomized events; cash/position/fees always reconcile to fills. Never assert profitable PnL on a single random path.
- **Edge cases from the brief §11:** no fills, one-sided continuous fills, inventory at limit, zero/high vol, price jump, wide/narrow/missing-side book, terminal timestep.

---

## 8. Experiment & Logging Protocol (condensed)

Every strategy claim comes from a named, reproducible experiment: hypothesis stated up front → change one dimension → many seeds → aggregate + dispersion + worst case → keep/reject with interpretation. Freeze code revision, config hash, dataset/scenario, seeds. Keep negative results.

Structured decision logs per event (see `MARKET_MAKING_AGENTS.md` for the full schema): run/config identifiers, market snapshot, `fair / reservation / raw and constrained spread / target quotes / binding constraints / decision reason`, order and fill fields, risk and accounting state. A final PnL number is never sufficient to diagnose a run.

---

## 9. Definition of Done (pre-Akuna)

The harness is sufficient when we can: run a seeded toy simulation; submit two-sided quotes; process fills into correct cash/inventory/marked PnL; run the fixed-spread baseline; add inventory skew; estimate realized vol; compute microprice/flow signals; record post-fill markouts; sweep parameters over many seeds; compare strategies on PnL **and** risk; stress-test high vol and one-sided flow; and swap any strategy component without touching the simulator or adapters — plus run the same quoter against RTG through the adapter.

## 10. Explicitly Deferred

No HJB solvers, full AS derivations, Hawkes processes, RL/neural agents, latency micro-optimization, or Akuna-specific assumptions until the baseline is measured and the real rules exist. A concept earns inclusion only as a fair-value feature, quote/spread/sizing rule, risk control, or experiment.
