# 2026-10-15 — Perpetual Alpha rules, Discord clarifications, and implementation gaps

**Project:** market-maker / Perpetual Alpha  
**Team:** Auror  
**Recorded / updated:** 2026-10-15 (SGT)  
**Primary source:** Organizers' Discord `#ask-anything` screenshots provided on 2026-10-15, including Giorgio's 2026-10-13 responses.  
**Guide:** https://perpetual-alpha26.github.io/docs/

> **Source convention:** "**Confirmed**" below means explicitly stated in the shared organizer screenshots, not independently verified against a later rule revision. "**Observed**" means checked via our own SDK diagnostic. "**Recommendation**" is an engineering inference, not a competition rule. Verify operational details against the latest organizer messages before switching to the scored environment.

## Execution milestone — first authenticated practice order-and-cancel (2026-10-15)

**Status: VERIFIED from the operator's Codespaces terminal screenshot**, following a local run of `apps/par_first_order.py` on branch `feat/par-first-order-smoke` ([draft PR #5](https://github.com/kyle-nguyennn/market-maker/pull/5)). This is an actual testnet exchange result, not an SDK mock or a dry-run. No credentials or private keys are recorded here.

| Item | Observed result |
| --- | --- |
| Venue | Hyperliquid **testnet**, practice DEX **`par`** |
| Market | `par:BTC` |
| Order | **BUY**, post-only limit (**ALO**) |
| Limit price | **80,835 USDPR/BTC** |
| Quantity | **0.0002 BTC** |
| Notional | **16.17 USDPR** |
| Submission response | **`resting`**, venue order ID returned |
| Cancellation | **CANCEL ACK: success**, using client order ID (CLOID) |
| Post-trade order check | **0 open `par:BTC` orders** |
| Post-trade position check | **0 `par:BTC` position** |
| Script result | **PASS: authenticated order, cancellation and flat-position verification** |

**What this proves:** API-agent signing with the team's trading account, internal order-intent to venue serialization, real `par:BTC` post-only order acceptance, cancellation by CLOID, and subsequent open-order/position REST checks all worked in this execution.

**What this does NOT prove:** fill/partial-fill handling, WebSocket event delivery or resynchronization, reliability after a real network outage, multiple concurrent orders, continuous quoting, emergency flattening, risk enforcement, or any authenticated order on scored `pa`. A resting order followed by successful cancellation is not a test of the fill path.

**Security qualification:** the execution command included the user's `--ack-key-rotated` acknowledgement. The terminal output does not independently verify the exposed old API key was revoked; continue to treat revocation as an operational prerequisite.

**Engineering next step:** implement `OrderManager.reconcile()` and enforce hard risk limits before introducing an automated quote loop. Keep this milestone separate from the scored `pa` environment's deployment/funding validation.

## TL;DR / immediate implications

1. **Practice `par` is not scored `pa`.** Practice collateral is **USDPR**; the live DEX is named **`pa`**, with **USDP** collateral. Do not reuse practice assumptions blindly.
2. On **2026-10-19**, organizers plan to halt **`par`**, automatically **settling outstanding practice positions at mark price**. The scored phase also starts on **2026-10-19**.
3. **`pa` has no historical candles prior to its deployment.** Bootstrap statistical signals from `par` or appropriate Hyperliquid mainnet references; collect real `pa` microstructure once it launches.
4. Funding is calculated as an **hourly settlement**, using the **oracle price**, with market-specific multiplier, interest and clamp parameters. The asset context `funding` field is only a running estimate.
5. A **reduce-only order closing the entire residual position** is permitted **below the usual $10 order minimum**.
6. Standard vs unified account modes have **different equity-query logic**. Size individual orders using **`activeAssetData.availableToTrade` / `maxTradeSzs`**, not equity alone.
7. Rate limits must shape quoting and cancellation architecture; there is also an additional **50,000 actions/day cap in unified mode** according to the organizer.

## 1. Practice vs scored/live DEX

| Parameter | Practice | Scored/live | Confidence |
| --- | --- | --- | --- |
| DEX | `par` | `pa` ("Perpetual Alpha") | Confirmed |
| Collateral | `USDPR` | `USDP` (**not** USDPR) | Confirmed |
| Symbol example | `par:BTC` | `pa:BTC` | Derived from confirmed DEX names |
| Phase | Practice until 19 Oct | Scored phase from 19 Oct | Confirmed |
| Phase transition | `par` halted, open positions settled at **mark price** on 19 Oct | Trade separately on `pa` | Confirmed |
| Markets | BTC, ETH, HYPE, SP500, EWY, EWJ, BRENT, GOLD, USDEUR, USDJPY | Same ten markets | Confirmed |
| Oracle / fee / funding parameters | Practice parameters | Same parameters as `par` | Confirmed |
| Historic candles | Practice history available for collection | No `pa` candles before first trades after deployment | Confirmed |

**Live DEX deployment timing:** The organizer's 13 Oct message said `pa` history would start from deployment "today/tomorrow max" (13–14 Oct). This is a **dated expectation**, not evidence that deployment completed or an authoritative 15 Oct launch timestamp.

**Do not infer the live network/host merely from the DEX name.** Confirm exact API URL, chain/network, approval requirements and wallet association from updated organizer instructions. Existing code hardcodes `https://api.hyperliquid-testnet.xyz`, `par` and `USDPR`.

### Engineering recommendations

- Replace hardcoded practice settings with a typed, **phase-aware venue profile**: `network`, `api_url`, `ws_url`, `dex`, `collateral`, instrument universe and explicit trading permission.
- Preserve unqualified internal symbols (e.g. `BTC`). Let the exchange adapter map `BTC` to `par:BTC` or `pa:BTC`.
- Validate `meta`, `metaAndAssetCtxs`, asset precision, open-order scope and wallet balance for the selected DEX before creating a signer.
- **Fail closed:** never send orders to `pa` until endpoint, authorizations, collateral and risk limits are independently confirmed.
- At the `par` halt, expect mark-price settlement; do not treat practice positions as transferred to `pa`.

## 2. Funding on `par` (same funding parameters stated for `pa`)

**Confirmed formula:**

```text
hourly_rate = multiplier * (P + clamp(I - P, -c, +c)) / 8

P = hourly average premium, derived from impact prices relative to oracle
I = interest term (8-hour convention)
c = market-specific clamp width
```

The premium `P` is **sampled every 5 seconds**, then averaged over the hour.

| Factor | BTC / ETH / HYPE | Other markets |
| --- | --- | --- |
| Multiplier | **1** | **0.5** |
| Interest `I` | Generally **0.01%** | Generally **0.01%**, except **0 for USDEUR and USDJPY** |
| Clamp `c` | **±0.05%** | **HIP-3 default**; confirm the numerical value from venue metadata/docs |
| Funding settlement | **Every hour at :00 UTC** | Same |

Additional confirmed details:

- The `funding` field in the asset context is the **running estimate for the current hour**, not a settled historical observation.
- For backtests and PnL reconciliation, use the **settled rates in `fundingHistory`**.
- Payment magnitude is **position size × oracle price × funding rate**; use **oracle, not mark**, at the relevant settlement.
- The organizer cites a Hyperliquid **hard cap of 4% per hour**, though the normal clamp/multiplier yields much smaller rates.
- Under the usual sign convention, a **positive** funding rate means **longs pay shorts**; verify sign handling against settled ledger events.

**Illustration (not a prediction):** If BTC has `P=0` and `I=0.01%`, then `hourly_rate = 0.01% / 8 = 0.00125%`. The payment magnitude on **$10,000 notional** is **$0.125/hour**.

### Engineering recommendations

- Maintain separate `estimated_funding_rate` and `settled_funding_rate` in domain data. Store settlement UTC timestamps and corresponding oracle valuation.
- Include estimated funding carry, fees and expected inventory holding time in reservation pricing and quote edge.
- Implement settlement-ledger reconciliation; do not backfill settled funding with the latest running estimate.
- Configure multiplier, interest and clamp **per market**, especially FX.

## 3. Minimum order rule and dust-position closes

**Confirmed:** A **reduce-only order that closes the entire position** is accepted even if its notional is **below $10**. The organizer illustrates a residual **0.0001 BTC** position. For a sub-$10 residual, **close the whole position**, not an arbitrary partial fraction.

**Example:** An IOC order partly fills and leaves ~$8 of BTC exposure. A reduce-only order for the *entire opposite-side residual* can close it even though it falls below the ordinary opening-order minimum.

### Engineering recommendations

- Distinguish `open_or_increase` from `full_reduce_only_close` before applying minimum-notional checks.
- Calculate close size from the **latest position**, correctly quantized to `szDecimals` (avoid flipping short/long or exceeding current exposure).
- Do not assume *all* reduce-only orders under $10 qualify: the stated exception specifically concerns **closing the entire position**.
- Monitor partial fills and residual dust; assert position flat after close confirmation.

## 4. Account equity, collateral, and market-by-market sizing

**Confirmed organizer guidance for the scored `pa` DEX:**

| Account mode | Equity calculation given by organizer |
| --- | --- |
| **Standard** | `clearinghouseState`, with `dex="pa"` → `marginSummary.accountValue` |
| **Unified** | `spotClearinghouseState`: **USDP balance + unrealized PnL on open positions** |

**Order-sizing API:** `activeAssetData` with **user + coin** → `availableToTrade` and `maxTradeSzs`. Organizer says these fields work in both Standard and Unified modes.

**Equity ≠ buying power.** Equity informs portfolio risk budget and drawdown; `availableToTrade` / `maxTradeSzs` reflect current per-asset trading capacity after margin/leverage/exposure constraints.

### Engineering recommendations

- Detect account mode explicitly; do **not** use practice `USDPR` balance as live `USDP` equity.
- Add a mode-specific `account_equity()` and separate `market_capacity(coin)` operation.
- In unified mode, implement the organizer's equity definition and validate it against the app/ledger before relying on it for PnL.
- Use Decimal throughout the internal equity, margin, position and risk model; convert SDK wire types only at the venue adapter.
- Reconcile account, collateral and position snapshots after fills and reconnects.

## 5. Market history and cold start

**Confirmed:** Candles for `pa` **start at its deployment/first trades**; there is no preexisting `pa` OHLCV history. The organizer points to **`par`** and **corresponding Hyperliquid mainnet markets** for historical reference.

### Engineering recommendations

- Record `pa` trades/books from first availability, including timestamps, spreads, depth and funding.
- Pretrain or bootstrap price features from `par` / related markets, but explicitly model **domain shift** in spreads, liquidity, participant behavior and fill probabilities.
- Avoid leaking practice DEX fills or liquidity assumptions into scored-live execution backtests.
- Make indicators robust to small lookback windows; degrade to simple conservative quoting when history is insufficient.

## 6. Request limits and action budgets

**Confirmed via Discord screenshot:**

| Limit | Stated rule |
| --- | --- |
| Starting address request budget | **10,000** |
| Volume-earned request budget | **+1 request per 1 USDC traded** (organizer's wording; verify denomination semantics for `pa` USDP collateral) |
| Once request budget exhausted | **1 request every 10 seconds** |
| Cancels | A **separate, higher** cancellation allowance; not unlimited |
| Batching | Batch with **N orders counts as N** |
| Unified account mode | Additional **50,000 actions/day** cap |
| Standard mode | Does **not** have the extra 50k unified-action daily cap |
| Position/notional caps | No special **per-team** on-chain caps stated; **market-wide open-interest caps, market-specific maximum leverage, ordinary order/request limits** still apply |

Note: No per-team on-chain position limit is **not** a waiver of our own risk limits, nor proof that there are no other organizer disqualification/risk rules.

### Engineering recommendations

- Centralize request throttling and budgeting; account for **per-order consumption in batches**.
- Throttle `place`/`amend` separately from cancels; reserve cancellation headroom for risk events.
- Prefer WebSocket market data to frequent REST polling; add quote hysteresis, minimum quote lifetime, price/size-change thresholds and bounded requote frequency.
- Avoid relying on the volume-credit mechanism to subsidize excessive churn.
- Model both the 10,000 + traded-volume budget and the extra unified-mode 50k/day cap.
- Track exchange rejections, quota errors and temporary slow mode as explicit execution states.

## 7. Codebase status and implementation gap — as of 2026-10-15

The following reflects the **SDK/read-only diagnostics achieved by 15 Oct**, not a claim that a full unattended trading system is ready. The reference baseline is the code in `market-maker` as reviewed that day.

| Component | Current state | Required change before scored-live use |
| --- | --- | --- |
| **Market-data SDK** | Working against `par` REST: metadata, contexts, L2, and WebSocket subscription primitives | Add verified `pa` configuration, subscribe/normalize streams, resync on reconnect |
| **Collateral query** | Reads practice **USDPR** using `spotClearinghouseState` | Read **USDP**; correctly handle Standard and Unified equity definitions |
| **Symbol mapping** | `par:BTC` and `dex=par` hardcoded or validated | Switch to `pa:BTC` and `dex=pa` through a phase-aware adapter/config |
| **SDK network** | Config and safety guard restrict use to testnet | Confirm actual live network/API/WS endpoints; explicit opt-in for signed live actions |
| **Historical data** | Can query practice snapshots; no comprehensive replay pipeline verified | Build collection, storage, historical references and cold-start behavior |
| **Funding** | Running estimate available from asset contexts | Query `fundingHistory`, reconcile settled funding and model expected carry |
| **Execution** | **Real signed `par:BTC` ALO resting-order acceptance, CLOID cancel and flat-position verification succeeded 2026-10-15**; partial/full fills not validated | Implement fill/partial-fill tracking, order reconciliation, and safe repeated quoting |
| **Risk** | Basic checks/interfaces; hard portfolio limits and reconciliation not fully wired | Exposure, notional, leverage, drawdown, stale-feed kill switch and request-budget safeguards |
| **Account sizing** | Practice state snapshots queried | Use `activeAssetData.availableToTrade` / `maxTradeSzs`, per-side and per-market |
| **Operational resilience** | SDK WebSocket subscription support | Add reconnect + resubscribe + book/account snapshot resync, event dedup and audit logs |

**Observed (earlier read-only diagnostic):** The funded Auror team wallet returned **1,000,000 USDPR** and valid `par:BTC` market-data normalization; no signed order was placed *in that earlier diagnostic*. The separate authenticated smoke test subsequently succeeded, as documented above. Do not record any private key in this note.

### Highest-priority tasks before 2026-10-19

1. **P0 — Environment switch:** verify `pa` deployment, correct network/REST/WS URL, supported symbols/precision, collateral asset and the team wallet's `USDP` funding; implement explicit practice/live profiles with a fail-closed switch.
2. **P0 — Security:** ensure the previously screen-exposed **API agent key has been revoked/replaced** before enabling signed operations; never commit credentials. Confirm the new API wallet is approved for the correct account and target environment.
3. **P0 — Order lifecycle:** **first signed practice ALO order, CLOID cancellation, and flat-position verification completed on 2026-10-15.** Next implement persistent order/fill reconciliation, partial-fill tracking and safe resynchronization. Verify sub-$10 *full close* behavior only if a genuine dust position exists; do not manufacture risk solely to test the exception.
4. **P0 — Risk circuit breakers:** implement strict max order notional, gross/net inventory, drawdown, leverage, stale-data cutoff and a kill switch. No unattended trading until these work.
5. **P1 — Quoting/reconciliation:** maintain durable client order IDs, reconcile fills/cancels/positions after disruptions and handle nested per-order API errors.
6. **P1 — Rate-limit accounting:** throttle requests per address and mode; build a bounded, hysteresis-based quote refresh loop.
7. **P1 — Data/carry:** capture real `pa` market history and settled funding; avoid treating practice snapshots as equivalent to live liquidity.
8. **P1 — Competition-transition behavior:** halt practice bot before `par` is stopped; independently initialize scored `pa` positions/balances rather than carrying practice state across.

## 8. Open questions to confirm with organizers

- What are the **actual scored-live REST/WS endpoints and network** for `pa`? Has the DEX been deployed?
- Has **USDP been allocated to Auror** for the scored phase, and is its wallet/account authorization already active there?
- Is the published **10k + 1/USDC traded** request allowance denominated equivalently on `pa` where collateral is USDP?
- What precise **HIP-3 default clamp** is used for the seven non-BTC/ETH/HYPE instruments?
- What is the exact **trading start timestamp (UTC)** on 19 Oct and the practice halt/cutover timing?
- Are there any **organizer-side risk, turnover, concentration or reporting requirements** beyond the on-chain market-wide limits?
- What are the precise **daily equity snapshot** and PnL scoring rules for the competition?

## References

- Organizer Discord `#ask-anything`, Giorgio's response on **2026-10-13**, as captured in screenshots shared in chat on **2026-10-15** (practice halt, dust close, history, equity, sizing and position limits).
- Organizer Discord `#ask-anything` announcement captured on **2026-10-15** (funding formula, live `pa` / USDP, request limits).
- Official participant guide: https://perpetual-alpha26.github.io/docs/
- Project integration reference: `docs/PERPETUAL_ALPHA.md`.
