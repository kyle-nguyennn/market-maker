# 2026-10-15 — Agent handoff: Perpetual Alpha order lifecycle, risk, baseline market maker

**Repository:** `kyle-nguyennn/market-maker`  
**Starting branch:** `feat/perpetual-alpha-order-risk-quoter` (**created directly from current `main`**)  
**Date:** 2026-10-15 (SGT)  
**Owner / team:** Auror, Perpetual Alpha  
**Mission:** implement **P0 order reconciliation**, **P0 hard risk controls**, then **P1 conservative market-making baseline** before the organizer's stated **2026-10-19** scored trading start.  
**Initial scope:** this handoff originally preceded implementation. The progress section at the end now records branch implementation and verification.

## 0. Instructions to the next coding agent

**This is an implementation handoff, not a request for another proposal.** Start by reading the files and reviewing existing tests, then implement in short, tested increments on **this branch**. Do not restart from an older `perpetual-alpha-scaffold` branch.

1. Treat `main` as the baseline containing **merged PR #4** (DEX-aware `par`/`pa` connectivity) and **merged PR #5** (successful `par` authenticated order-and-cancel smoke).
2. Keep internal strategy, order, risk, and accounting models **venue-independent**; `Decimal` for all price/quantity/money/rate values; internal symbol `BTC`, venue symbol `par:BTC` or `pa:BTC` only at the adapter.
3. Maintain a clear sequence: **normalized event → account/order state → risk evaluation → desired quotes → risk-vetted intents → reconciliation → bounded actions → audit log**. Continue processing fills and cancels while quoting is disabled.
4. Default all new executable modes to **paper/dry-run**. **Never** send an authenticated order, switch on continuous live trading, flatten positions or enable `pa` trading without explicit operator authorization. No API key in GitHub, logs or screenshots.
5. Existing safety configuration **`PERPETUAL_ALPHA_DEX=par|pa`** is the one DEX selector. Do **not** reintroduce a `phase` selector. On `pa`, the existing `PERPETUAL_ALPHA_PA_ALLOW_TRADING=false` guard must remain effective.
6. Add deterministic unit tests, mocked SDK integration tests, and a short demonstration output for each delivered milestone. Update this note with checkboxes, commits, CI links and any blocked assumptions for the next agent.
7. Keep PR scope cohesive, preferably independent commits for normalized lifecycle/events, risk, conservative quoter, and composition runner. Raise uncertainties explicitly; do not silently guess exchange behavior.

### Verified baseline as of 2026-10-15

- Real Hyperliquid **testnet `par:BTC`** authenticated **BUY ALO** of **0.0002 BTC at 80,835 USDPR** (~**16.17 USDPR** notional) was acknowledged **`resting`**, cancelled **by CLOID** with **`success`**, and REST verification returned **zero open BTC orders and zero BTC position**. This validates resting-order/cancel wiring, **not** partial fills or a working quoting loop.
- Read-only SDK connectivity, per-DEX metadata, book/context normalization to `Decimal`, and master-account queries exist. The latest recorded check on PR #5 (after PR #4 integration) ran **55 tests with `par` selected and 55 with `pa` selected**.
- `par` uses **USDPR**, `pa` uses **USDP**; **both profiles currently point at Hyperliquid testnet**. The latest observed `pa` discovery listed `pa:BTC`, `pa:ETH`, `pa:HYPE` only, and the `pa:BTC` book did not yet have a valid two-sided quote. Recheck before assuming readiness.
- Organizers stated **`par` halts on 2026-10-19**, settling open positions at mark, while `pa` is the scored DEX. Do not carry practice positions into the scored account.
- Security: an earlier API-wallet private key was visible in a screenshot; a CLI flag acknowledged key rotation, **but revocation of the exposed key has not been independently verified**. Do not access or print any key in this task.

**References:** `docs/PERPETUAL_ALPHA.md`, `docs/PAR_FIRST_ORDER_SMOKE.md`, `notes/20261015_perpetual_alpha_competition_rules.md`, `model/AGENTS.md`, and `docs/MARKET_MAKING_AGENTS.md`. Older RTG/Akuna notes are architectural references, **not authoritative Perpetual Alpha venue rules**.

## 1. Code inventory — do not duplicate work

| Area | Existing file(s) | Current capability / gap |
| --- | --- | --- |
| DEX selection | `exchanges/hyperliquid/config.py`, `.env.template`, `config/perpetual_alpha.yaml` | Validated `par`/`pa` profiles and collateral separation. `pa` order signing disabled by default. |
| Hyperliquid API | `exchanges/hyperliquid/client.py`, `types.py` | REST book/meta/positions/open orders/fills/order status; signed one-order submit/cancel; raw WS subscription hooks. Not a resilient execution runtime. |
| Venue adapter | `market_handler/hyperliquid_adapter.py` | Converts L2 to `Decimal MarketState`; converts `OrderIntent` to venue request with precision rules; maps SDK order result. |
| One-shot proof | `apps/par_first_order.py`, `tests/test_par_first_order.py` | **Verified** testnet ALO place/cancel and flat-state checks. Reuse protocol learnings; **do not turn it into an unattended loop**. |
| Domain orders | `model/order.py` | `Side`, `Quote`, `OrderIntent`, minimal `ExecutionReport`. No fill-event/cancel-event taxonomy. |
| Order lifecycle | `execution/order_manager.py` | `LiveOrder` and state enum exist; **`OrderManager.reconcile()` raises `NotImplementedError`**; no pending-request suppression/dedup/resync. |
| Risk | `risk/risk_manager.py` | `RiskLimits`, `RiskDecision`; only market-age, single order-notional, basic kill-switch flag. Missing exposure, drawdown, capacity and on-path enforcement. |
| Accounting | `portfolio/accounting.py` | One-instrument cash/position/fees and marked equity; no fill-ID dedup, realized/unrealized split, multi-market exposure or funding reconciliation. |
| Quoter | `quoter/base_quoter.py` | `BaseQuoter` / `BaseMMQuoter` contract; no concrete BTC quoter, controlled quote cadence or execution runner. |
| Market model | `model/market.py` | `Decimal` order book / mark / oracle / funding fields and validity flag. Add freshness/sequence semantics through a deliberate data-quality interface. |
| Tests/CI | `tests/`, `.github/workflows/hyperliquid-sdk-tests.yml` | SDK, venue boundary and one-shot contract tests. Add deterministic state-machine, risk, simulation/replay and runner tests. |

**Known stale guidance:** `exchanges/hyperliquid/AGENTS.md` still describes `par` as the only DEX; reconcile it with **merged PR #4** so future agents do not regress `pa` support. Do not equate setting `PERPETUAL_ALPHA_PA_ALLOW_TRADING=true` with operational readiness.

## 2. P0-A — Complete the order lifecycle and reconciliation

**Objective:** deterministically translate **desired target orders** into typed **venue-neutral new/cancel actions**, accounting for acknowledged, pending, partially filled, rejected and uncertain orders, without duplicate orders or crossing while replace/cancel is in flight.

**Suggested locations:** `model/order.py` (events/actions as needed), `execution/order_manager.py`, `tests/test_order_manager.py`; venue event translation in `exchanges/hyperliquid/` or `market_handler/` (never in the pure order manager).

### Required behavior

1. Define typed `PlaceAction`, `CancelAction` (and if needed `NoOp`), durable `client_order_id` (CLOID) and mapping between CLOID and venue `oid`. A method such as `reconcile(desired: list[OrderIntent]) -> list[OrderAction]` should remain pure or explicitly separate state transitions from dispatch.
2. Model order lifecycle: `PENDING_NEW -> RESTING -> PARTIALLY_FILLED -> FILLED`, `PENDING_CANCEL -> CANCELLED`, `REJECTED`, plus explicit `UNKNOWN/NEEDS_RECONCILIATION` on ambiguous request timeouts. Do not treat cancel ACK as evidence that no fill occurred.
3. No duplicate submissions for the same logical quote while `PENDING_NEW`; don't recreate a quote while a cancel or previous submit is unresolved. Deterministic idempotency for repeated/restored callbacks and duplicate fill notifications.
4. Handle **cumulative filled size** vs **per-fill delta** explicitly; maintain `0 <= remaining_size <= original_size`. Do not debit inventory twice. A partial fill may precede cancel acknowledgement; account for that race.
5. On desired quote unchanged after venue quantization, return **no action**. On price/size change, **cancel old order, await state resolution, then place replacement**; avoid overlapping same-side quotes until risk explicitly allows it.
6. On reconnect/startup, **stop new orders**, fetch open orders, order statuses/fills and account positions, then reconstruct state; resume only when reconciliation is complete. Unknown historical/foreign orders must not be silently adopted or cancelled without a documented policy.
7. Track actions and outcomes in an audit log (timestamp, `dex`, symbol, CLOID, oid, state, reason; no keys). If an ACK fails, distinguish **rejected**, **unknown** and **confirmed not live**.
8. Preserve and extend existing one-shot `place_order`, `cancel_order`, `query_open_orders`, `query_order_status`, `query_fills` rather than reinventing the Hyperliquid API. Normalize raw WS `orderUpdates` and `userFills` into domain events; verify SDK message shapes with fixtures or captures, not guesses.

### Acceptance tests (must automate)

- Unchanged desired quote + resting order -> no action; duplicate callbacks -> no inventory/order count change.
- Desired quote removed -> one cancel, repeated reconciliation before cancel ACK -> no second cancel.
- Desired quote changed -> cancel -> wait -> replace; no simultaneous same-side duplicates.
- Out-of-order `RESTING`, partial fill, pending cancel and filled/cancelled transitions resolve correctly.
- Ambiguous submit timeout -> **no retry with a different CLOID** until order-status/open-order reconciliation resolves it.
- Duplicate fill ID / replayed stream -> exactly-once portfolio update (dedup by venue fill ID or equivalent grounded key).
- Startup snapshot differing from local in-memory state -> quarantine/resynchronize before quoting.
- Cross-DEX or wrong-account order updates must never contaminate local state.
- State-machine invariants and action ordering under deterministic event replay.

**Definition of done:** a deterministic fixture/replay demonstrates **new → resting → cancel → confirmed closed**, **partial fill**, and **lost ACK/resync**, with no duplicated exposure.

## 3. P0-B — Hard risk controls ON THE ACTUAL SEND PATH

**Objective:** no new order can bypass pre-send risk checks; risk must account for **current position plus resting/pending fillable orders**, not just a proposed order in isolation.

**Suggested locations:** `risk/risk_manager.py`, `portfolio/accounting.py`, a narrow `execution/` dispatcher/gateway, `tests/test_risk_manager.py`, `tests/test_safe_execution.py`, and validated limits in `config/perpetual_alpha.yaml`.

### Required controls

1. **Order validity:** positive finite Decimal prices/sizes, active venue metadata/precision, non-crossing post-only quotes, per-order maximum notional, venue min notional (noting the organizer's **under-$10 full-position reduce-only close exception**), market/DEX whitelist and funding/collateral readiness.
2. **Worst-case positions:** evaluate signed exposure if *all* live plus pending bids fill, and independently if *all* asks fill; guard max absolute position, gross notional, net exposure and per-symbol caps. Count existing positions, pending placements and unacknowledged cancels as still fillable.
3. **Capacity:** get `activeAssetData` user+coin `availableToTrade`/`maxTradeSzs`, and account equity appropriate to **Standard vs Unified** margin mode as organizer described. These SDK queries are **not yet wired into the client**; validate API shapes first. Failure or stale capacity data -> block new orders, not cancellations.
4. **PnL/drawdown:** define baseline equity, realized/unrealized PnL, fees and funding treatment; kill new quoting on breached drawdown (no magic defaults; choose explicit conservative config values and document units/thresholds).
5. **Market-data quality:** halt new orders and request appropriate cancels when book has missing/crossed sides, timestamp is stale/future, quote/reference price diverges outside a configured collar, or feed reconnects without resync.
6. **Emergency kill switch:** latch on risk breach/operator command; block all **new/amend** actions while still allowing/carrying out cancel actions; do not automatically unlatch. A failed cancel or unknown venue state must be surfaced and retried within a bounded policy, never declared safe.
7. **Request budgets:** use explicit per-address throttling and backlog/quoting limits. Organizer's Discord guidance: initial 10,000 request budget; +1 per 1 USDC traded; 1 request / 10 s after exhaustion; batches count per order; cancel budget separate; unified mode adds 50,000 actions/day cap. Confirm any denomination/detail before exact production limits.
8. Enforce the final checks **after** adapter quantization and **immediately before** the SDK send, not merely at quote construction. The quoter must not be able to call SDK order submission directly.

### Acceptance tests

- Reject order when market data is stale, crossed, missing a side, NaN/non-positive or wrong DEX; no SDK order method invoked.
- A current position plus two fillable resting bids crosses max exposure -> reject further bid even if new order alone is under limit.
- Pending cancellation is **still counted** in worst-case exposure until terminal confirmation.
- A buy may be blocked while a risk-reducing sell is allowed, when consistent with reduce-only semantics; **kill switch cancels first**, prevents new exposure.
- Insufficient available-to-trade / missing account mode / unknown collateral -> halt placements, allow cancels.
- Drawdown breach latches kill switch until explicit operator reset after clean reconciliation.
- Rate-limit exhaustion delays placements and preserves cancellation headroom.
- Failure injection: disconnect/timeout between risk-check and send -> fail closed, reconcile before retry.

**Definition of done:** in the runner, even a malicious/faulty quoter cannot send an order violating risk limits; a kill switch can cancel outstanding quotes without authorizing new placements.

## 4. P1 — Conservative, deterministic baseline market maker

Only begin authenticated continuous quoting after P0-A and P0-B work and the scored venue is separately verified. Implement a **paper/replay mode first**.

**Suggested files:** `quoter/conservative_mm.py`, `tests/test_conservative_mm.py`, and a separate `apps/perpetual_alpha_runner.py` (or equivalent thin orchestrator). Preserve `BaseMMQuoter` as the pure strategy contract; don't move SDK code into strategy modules.

### Baseline strategy specification (recommended defaults, not competition rules)

- **Reference/fair value:** choose and log a configurable reference among **oracle, mark, mid**, or a documented blend. Require a valid, recent two-sided L2 book. Prefer simple deterministic behavior if data history is short.
- **Reservation price:** `r = s - q * kappa` using a clearly specified inventory-skew coefficient with consistent units and exposure bounds. This is a deliberate heuristic, **not** the canonical Avellaneda–Stoikov coefficient. Move to `r = s - q * gamma * sigma^2 * tau` only after volatility units/horizon are calibrated.
- **Half-spread floor:** account for fees, estimated slippage/adverse selection, latency and a nonzero safety buffer; `bid = r - h`, `ask = r + h`. Limit prices must stay non-marketable for ALO. Recompute risk **after** side-aware price rounding.
- **Sizing:** smaller quotes as inventory approaches its limit, with one order per side initially; suppress risk-increasing side at cap. Respect `szDecimals`, $10 minimum normal opening order and a configurable per-order/notional cap.
- **Quote hysteresis:** don't cancel/requote for every book tick. Only refresh when quantized desired quote changes beyond a configured ticks/bps threshold, age exceeds a bounded max or risk needs cancellation. Preserve min quote lifetime except for urgent risk events.
- **Cadence / request budget:** throttle all order actions by address. Prefer WS subscriptions; use REST snapshots for startup/resync. Avoid a polling loop that exceeds API allowance.
- **Inventory and PnL:** mark positions consistently (document oracle vs mark choice), record signed inventory, fees, carry/funding estimates and spread capture. The live `pa` asset context funding is an estimate, not an hourly settlement.
- **Operational modes:** (1) deterministic offline replay; (2) live market-data **paper** decisions; (3) manually authorized capped testnet quoting only once safety criteria pass. No auto-selection of live mode from an old `.env`.

### Acceptance tests

- Flat inventory -> ordered non-crossing two-sided quotes above min edge.
- Long inventory -> reference/reservation price shifts downward, bids become less attractive / asks more attractive; short inventory does the converse.
- At exposure cap -> do not increase exposure; reduce/omit risky side.
- Stale/crossed/missing books -> no placements; existing quote cancellation requested.
- Tiny market changes -> no churn; significant moves -> bounded cancel/replace.
- Actual adapter-quantized prices/sizes remain passive and within all limits.
- Parameter sweeps/replay across fee and spread conditions demonstrate execution behavior, not claimed profitability.

**Definition of done:** an end-to-end paper replay of `MarketState -> quoter -> RiskDecision -> OrderManager actions` with deterministic, auditable outputs, plus an operator-controlled and bounded integration mode. Never claim strategy profitability from the one-order smoke test.

## 5. Sequencing, incremental deliverables and checkoffs

- [ ] **A1 — Domain lifecycle events/actions:** normalized events, CLOID/OID identity and deterministic state transitions, with fixtures/tests.
- [ ] **A2 — Reconciliation:** `OrderManager.reconcile()`, pending-action suppression, duplicate/out-of-order updates and startup snapshot recovery.
- [ ] **A3 — Safe gateway:** adapter conversion, pre-send risk evaluation and action dispatcher; no SDK ordering from the quoter.
- [ ] **B1 — Risk limits:** exposure including pending orders, capacity, drawdown, stale-feed handling, kill switch, config validation and unit tests.
- [ ] **B2 — Kill-switch/recovery simulation:** failure injections for lost ACK, stale WS, venue rejection and cancel timeout; prove halt/cancel/restore flow.
- [ ] **C1 — Baseline strategy:** pure deterministic quoting with reference, inventory skew, fees/spread floor, sizing and hysteresis.
- [ ] **C2 — Paper runner:** live market-data consumption, state updates, logging, replay and metrics; **no authenticated writes by default**.
- [ ] **C3 — Operator-controlled test:** limited approved `par` practice exercise before halt **if still available**, or comparable explicitly authorized `pa` test **only after** scored funding, two-sided liquidity, all risk controls and API authorization verified.
- [ ] **Docs / CI:** update `docs/PERPETUAL_ALPHA.md`, `exchanges/hyperliquid/AGENTS.md`, and this handoff with exact passing tests, measured behavior and known defects.

### Suggested implementation cadence

**October 15–16:** A1/A2, deterministic order tests and initial gateway.  
**October 16–17:** A3/B1/B2, risk/failure-injection tests.  
**October 17–18:** C1/C2 conservative paper baseline, paper replay and `pa` discovery/funding checks.  
**October 18–19:** only if fully green, manually approve bounded execution; otherwise stay paper/read-only.

This is a **target schedule**, not a guarantee; do not skip invariant tests to hit dates.

## 6. Scored-`pa` go/no-go gates

Do **not** enable unattended `pa` quoting unless all are true:

- `pa` REST+WS are reachable, metadata and precision are correct for target symbols, and its book is liquid, two-sided and fresh.
- Registered Auror team account has actual **USDP** scored collateral; agent signer is approved for correct account, with exposed old key revoked; signing permission explicitly enabled **only with operator consent**.
- Correct margin mode/available-to-trade is queried; hard risk limits and kill switch are enforced on the send path.
- Open orders, fills and positions resynchronize after cold start or lost connection; idempotency and uncertain-order quarantine are tested.
- Rate limits, cancel headroom, funding, fees and price/size precision are accounted for.
- Structured logs, alerts, manual shutdown and a **paper mode** exist; no unknown live orders/exposure at startup.

If a gate is unknown or false, **block new orders** and continue read-only validation or paper trading.

## 7. Important hazards and open questions

- **State reconciliation vs snapshot races:** REST and WS can arrive out of order; define a snapshot anchor + dedup/version approach, not naive last-write-wins.
- **No fill-path proof yet:** PR #5 only verified a resting order and cancel. Simulate partial fills and venue event ordering before live automation.
- **Risk mode ambiguity:** organizer described distinct account-equity logic in Standard vs Unified. Validate SDK-specific response fields; never infer equity solely from spot wallet balances across modes.
- **MIN order:** organizer confirmed an exception for a **reduce-only full-position close below $10**; do not globally disable minimum size checks for reduce-only partial reductions.
- **`pa` deployment:** the observed three-market/empty-BTC-book status is a snapshot from October 15, not a guarantee about October 19 readiness. Re-query.
- **`par` phase-out:** do not create persistent practice positions just to exercise fills when halt/mark settlement is imminent.
- **File scope:** the old `docs/MARKET_MAKING_AGENTS.md` was originally RTG/Akuna-oriented; import its portable architecture, state and calibration discipline, not RTG tick or fee assumptions.
- **Existing `RiskLimits` config has `null` placeholders:** these are not safe production defaults; fail closed rather than treating `null` as infinity.
- **Operator actions:** do not expose cancellation and flattening as the same action. A filled order is a position, and cancel does not flatten.

## 8. Next agent's very first command sequence

~~~bash
git fetch origin
git switch feat/perpetual-alpha-order-risk-quoter
python -m pip install -r requirements/hyperliquid.txt
python -m pytest -q
python -m apps.perpetual_alpha --check --dex par
# Optional, READ-ONLY: python -m apps.perpetual_alpha --check --dex pa
~~~

Then inspect, in this order:

`model/order.py` → `execution/order_manager.py` → `exchanges/hyperliquid/client.py` → `market_handler/hyperliquid_adapter.py` → `risk/risk_manager.py` → `portfolio/accounting.py` → `quoter/base_quoter.py` → `config/perpetual_alpha.yaml` → existing tests.

**First coding target:** implement typed order lifecycle events/actions and replace `OrderManager.reconcile()`'s `NotImplementedError` with deterministic cancel/wait/replace semantics. Add the edge-case tests **before** connecting it to live SDK order submission.

### Agent handback format

When pausing or handing work off again, append:

- Commit SHA / PR / current test run.
- Exactly which checklist items are done (and any behavior changed).
- Tests actually executed, including failures.
- Any active orders/positions **only if verified from account queries**, otherwise say not checked.
- Known unknowns and one concrete next command or file to implement.
- Confirmation that no secrets were logged and whether any authenticated exchange actions were deliberately performed.


---

## 9. October 15 continued agent execution — PR #6

**Branch:** `feat/perpetual-alpha-order-risk-quoter`  
**PR:** [#6 (draft)](https://github.com/kyle-nguyennn/market-maker/pull/6)  
**Last code increment:** `f4f339f2f1994272f1a3c36fd6b099e580fb80ef` (scoped read-only capacity query test).  
**Latest all-code green CI:** [run 38716874167](https://github.com/kyle-nguyennn/market-maker/actions/runs/38716874167), **123 passed** with the default `par` selection and **123 passed** with `PERPETUAL_ALPHA_DEX=pa`. SDK testnet read-only probe jobs were skipped because this was a pull-request run.

### Completed in this continuation

- [x] **A1 domain primitives (pure portion):** `OrderEventType`, `OrderEvent`, `PlaceAction`, `CancelAction` with 128-bit hexadecimal CLOIDs and cumulative-fill semantics. **Raw WS event parsing is NOT implemented.**
- [x] **A2 pure order manager:** `OrderManager.reconcile()` no longer throws. Supports one quote per symbol+side, no duplicate while pending, cancel/wait/replace, partial cumulative fill/late duplicate handling, send/cancel uncertainty quarantine, explicit fail-closed snapshot/status recovery, and manual emergency cancellation of known resting orders. **Startup REST/WS orchestration and durable journal are NOT implemented.**
- [x] **A3 mock-validated gateway core:** `execution/safe_gateway.py` defaults to dry-run, checks final quantized request, verifies account/DEX/intent against reservation, and evaluates all still-fillable orders sourced from OrderManager. Mocked live SDK placements, cancels, timeouts and kill switch have deterministic tests. **Do not treat this as authorization for live use.**
- [x] **B1 pure risk model:** `RiskManager.evaluate_placement()` enforces finite positive prices/sizes, passive post-only prices, stale/future/invalid book, price collar, min/max notional including full reduce-only close exception, worst-case long/short position and gross/net limits, verified equity/margin/capacity fields, and latched absolute drawdown kill. `null` required thresholds block placement.
- [x] **Fill-ID dedup in single-instrument accounting proxy:** new `seen_fill_ids` prevents REST/WS replay from double-applying unique per-trade fill IDs; supports negative fees (rebates).
- [x] **Docs** updated: `docs/PERPETUAL_ALPHA.md` and `exchanges/hyperliquid/AGENTS.md` now distinguish practice `par` from scored `pa`.
- [x] **Read-only `activeAssetData` request** wired through `HyperliquidVenueClient.query_active_asset_data(team_wallet, instrument)` with selected-DEX symbol and master-account address. **No capacity conversion, margin-mode equity integration or live account query has been verified.**

### Safety and incomplete acceptance gates

- Current `RiskSnapshot.capacity_buy/capacity_sell` expects **base-asset quantities**. Official `activeAssetData.availableToTrade` represents **notional**, while `maxTradeSzs` and precise account margin semantics require an explicit confirmed conversion policy. **Never feed raw availableToTrade numbers directly into these fields.** Source: Hyperliquid's `activeAssetData` info endpoint documentation. Until mapped and tested, the snapshot builder must pass unavailable capacity (blocks new orders).
- Current runner does not create authoritative `RiskSnapshot` from verified USDPR/USDP account state and `activeAssetData`. No continuous runner exists.
- No durable action-before-send journal, atomic dispatch lock, callback dedup storage, reconnect snapshot anchor, or exact raw SDK `orderUpdates`/`userFills` WS translator yet.
- Rate-limit budget, reserved cancellation headroom, stale-feed cancellation orchestration, and bounded cancel retry/recovery remain missing.
- No baseline quoting strategy, offline paper-runner loop, or `pa` funds/book readiness verification.
- Do not merge this PR as live-ready. Existing `PERPETUAL_ALPHA_PA_ALLOW_TRADING=false` guard remains unchanged. Real account orders/positions were **not queried** in this continuation.
- Authenticated venue actions performed by this agent: **none**. No credentials read or printed. Integration tests use an offline fake SDK client only.

### Focused next steps (in order)

1. Build `exchanges/hyperliquid/` **fixture-validated event normalization** for confirmed REST status shapes and WS `orderUpdates`/`userFills`, with explicit side/DEX/wallet validation and fill-ID dedup; do not infer unknown shapes.
2. Implement **persistent action journal** written before network sends, startup snapshot anchor, position/fill replay, and reconciliation status checks. Ensure unknown CLOIDs cannot escape quarantine.
3. Add `activeAssetData` -> **confirmed** base-size capacities + Standard/Unified equity adapter, including stale/failing reads. Wire one authoritative snapshot provider to `SafeExecutionGateway`.
4. Implement request budgets, kill-triggered cancel dispatch/retry and a small deterministic paper replay. Do NOT enable live mode until those are green.

**Next local test command:** `python -m pytest -q tests/test_order_manager.py tests/test_safe_execution.py tests/test_risk_manager.py tests/test_portfolio_fills.py tests/test_hyperliquid_client.py`. Also test with `PERPETUAL_ALPHA_DEX=pa`. Open PR #6 checks to confirm the final head is green before continuing.
