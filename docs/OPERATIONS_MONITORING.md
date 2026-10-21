# Workstream: Operations & Monitoring (OM)

**Project:** market-maker  
**Status:** Partially implemented — OM-0/1/2 primitives merged in PR #9; durable SQLite execution and PAS MVP runner merged in PRs #10/#11. No end-to-end monitoring deployment or unattended-trading signoff.  
**Scope:** Shared across Perpetual Alpha / Hyperliquid and SIG Predictions Cup  
**Created:** 2026-10-21 (SGT)  
**Priority:** P0 for safety/reconciliation, P1 for telemetry and strategy analytics, P2 for reports and polish  
**Owner:** Trading-system operator / market-maker project

## 1. Mission

Make unattended trading *observable, diagnosable, and safe*: reliably answer (1) is the bot behaving correctly, (2) what risk is on, (3) what caused P&L and execution outcomes, and (4) how is the bot performing against each competition's objective.

A healthy process is **not** evidence of healthy trading. Infrastructure, execution correctness, risk, strategy performance, and competition score are separately measured.

### Guiding principles

- **Risk checks are synchronous and fail-closed** in the order path. Telemetry publishing, dashboards, alert delivery and storage must not be required for risk enforcement.
- **Venue adapters normalize events**; the shared trading system and analytics use venue-neutral concepts and Decimal financial values.
- **Venue/account/instrument isolation** is mandatory on all metrics, events, state and reconciliation queries; avoid mixing Perpetual Alpha practice (par) and scored (pas), or different SIG tournaments.
- **Exchange/account state is authoritative** for open orders, fills, balances and positions; local order state is reconciled against it. A network timeout is *unknown*, not a failure or a success.
- **Preserve the decision trail**, including no-trade decisions, model versions and contemporaneous market state, for causal diagnosis and replay.
- **Actionable alerts rather than noisy alarms**: page only for risk or execution failures; deliver strategy quality as periodic analytics.

## 2. Architectural boundaries

~~~
Hyperliquid (par / pas)          SIG Predictions v1 API
          |                               |
          +---------- venue adapters ------+
                         |
             normalized domain events
        market | order | fill | account | risk
                         |
            strategy + OrderManager + risk
                         |
             nonblocking event publisher
                  /            \
          metrics exporter     append-only event journal
               |                |
         Prometheus         Postgres / Parquet
               |                |
        Grafana / Alerts    P&L, attribution, replay
                  \            /
           operator reports / mobile notifications

Independent watchdog + reconciliation controller
       -> trading gate / cancel / manual escalation
~~~

**The diagram above is the target architecture, not a deployed topology.** Prometheus/Grafana/Alertmanager and Postgres/Parquet analytics are not deployed, and the PAS runner does not currently publish to the generic OM event journal.

Existing repository anchors:
- docs/PERPETUAL_ALPHA.md: DEX-aware Hyperliquid integration and recovery gaps.
- execution/order_manager.py: merged fail-closed venue-neutral lifecycle (PR #6), including order reservations, uncertain outcomes and explicit resynchronization. Preserve this canonical state machine.
- risk/risk_manager.py and execution/safe_gateway.py: synchronous pre-trade risk enforcement; monitoring can supervise but must not replace the actual send-path gateway.
- exchanges/hyperliquid/: venue-specific order, book, funding and account normalization.
- SIG Predictions adapter: PR #12 (open at audit) ports the older SIG scaffold in PR #2 onto the current core. SIG monitoring and scored runtime integration are not merged/deployed.

**Merged implementation baseline (2026-10-21):**
- **PR #9:** `observability/events.py` (scope, redaction, Decimal), `observability/journal.py` (bounded async **diagnostic** JSONL; NOT transactionally durable), `observability/reconciliation.py` (scoped snapshot observer), `observability/supervisor.py` (`PAUSED/ACTIVE/STOPPED`, operator re-arm), `observability/watchdog.py` and `apps/ops_watchdog.py` (heartbeat writer and one-shot checker). Offline tests: `tests/test_observability_p0.py`. Details: `docs/OPERATIONS_MONITORING_P0_MVP.md`.
- **PR #10:** `execution/sqlite_journal.py` is the **durable** single-writer SQLite WAL order/fill/kill-switch ledger. `execution/durable_dispatch.py` commits prepared placements before passing them to the risk-checked gateway. These are execution persistence components, **not** replacements for diagnostic JSONL.
- **PR #11:** `execution/pas_mvp0_runtime.py` and `apps/pas_mvp0.py` implement one-process PAS startup and periodic REST reconciliation, single-owner OMS, background quoter, SQLite journal, and trading gates. Scored unified equity is calculated from **USDP spot total plus DEX-scoped unrealized P&L**. Default is dry-run; see `docs/PAS_MVP0_RUNBOOK.md`.
- **Integration gap:** PAS MVP-0 uses its own reconciliation gate and the existing `SafeExecutionGateway`/SQLite journal; it does **not** instantiate the generic `SafetySupervisor`, `AccountReconciler`, `AsyncEventJournal`, or heartbeat writer. An independent scheduled watchdog and external alert delivery are not connected. These implemented primitives must not be mistaken for deployed controls.

Dependencies: OrderManager state machine, fill/position normalization, streaming resync, and venue-scoped risk limits. This workstream is distinct from execution/risk gateway development, but shares its contracts.

**Do not place sensitive keys in metrics, labels, event payloads or dashboards.** Log hashed/redacted account identifiers where full values are unnecessary.

## 3. Telemetry contract (OM-0)

### 3.1 Shared event envelope

**Target contract** for the completed event pipeline (the current `OperationalEvent` does not yet contain every field below):

| Field | Purpose |
| --- | --- |
| event_id | Globally unique/deduplicable event identifier |
| observed_at_utc | Local receive timestamp, UTC |
| venue_event_at_utc | Venue/event timestamp when supplied |
| venue, environment | sig or hyperliquid; scoped target (practice/scored/tournament) |
| account_id, tournament_id | Stable scoped identifiers, redacted where appropriate |
| instrument | Internal canonical instrument; never implicitly cross-venue |
| event_type | market, decision, order_intent, order_ack, order_update, fill, balance, position, funding, risk, control |
| correlation_id | Link decision -> order -> execution -> reconciliation |
| model_version, config_hash, deployment_sha | Reproduce the decision and runtime |
| payload_schema_version | Event schema compatibility |

**Current PR #9 event schema:** `OperationalEvent.to_record()` emits `schema_version=1`, `event_id`, `correlation_id`, `event_type`, `occurred_at`, sanitized `scope` (venue, environment, hashed account, tournament), `config_hash`, `deployment_sha`, and `payload`. Venue-event timestamp, `model_version`, a top-level `instrument`, and the specification's exact `observed_at_utc` / `payload_schema_version` names are **not present as top-level fields**. Version or map the envelope explicitly before implementing downstream consumers. No indexed event-ID deduplication/replay contract exists.

Payloads use **Decimal** for persisted monetary values/prices/quantities (serialize exact strings); numeric observability aggregates can use bounded floating-point representations. Never make Grafana/Prometheus a source of truth for balances.

### 3.2 Decision journal

Log snapshots/feature references, fair value, theoretical edge, inventory, risk constraints, intended quote/action, action suppressed reason, client order/idempotency key, venue response and fill/outcome linkage. Log **hold/no-trade** decisions at a controlled sampling rate as well as executed ones.

The raw decision/event store must support deterministic replays and comparisons by model/config SHA. Do not place high-cardinality order IDs or client IDs into Prometheus labels: store them in logs/event DB instead.

### 3.3 Instrumentation split

- Metrics: bounded-label Prometheus counters, gauges, histograms for dashboards and alert evaluation.
- Structured append-only events: **target** order/fill/risk/decision/reconciliation history in Postgres or Parquet; **current** diagnostic sink is best-effort async JSONL, with separate transactional SQLite WAL for execution identities/fills.
- Application logs: searchable errors and lifecycle diagnostics containing correlation IDs.
- Optional distributed traces only when diagnosing bottlenecks; not a requirement for MVP.

## 4. Monitoring coverage

| Layer | Metrics / evidence | Sampling / trigger |
| --- | --- | --- |
| Runtime | process heartbeat, uptime, restart count, CPU/memory, crash/exception count | periodic + process lifecycle |
| Market data | last venue update age, snapshot age, sequence/revision gaps, resync count, BBO validity, spread/depth, volatility | every update + freshness timer |
| Connectivity | WS disconnected duration, reconnect attempts, REST failures, auth errors, token expiry | every transport event |
| Execution | order lifecycle states/counts, time to ack, cancel-to-confirm time, fill latency, reject rate, in-flight unknowns, retries, request budget | every order event |
| State integrity | local vs venue open order / position / cash differences; age of last successful authoritative reconciliation | every fill, reconnect, periodic |
| Risk | position and open-order worst-case exposure, gross/net notional, equity, drawdown, concentration, margin headroom (where relevant), pending cancel exposure | every risk-changing event + periodic |
| Trading quality | fill rate, maker/taker split, effective/realized spread, adverse-selection horizons, inventory holding time, fee/funding drag | fills + scheduled aggregation |
| Alpha/scoring | expected edge, realized P&L, calibration, regime attribution, competition-specific progress | scheduled aggregation / settlement |

**No-fill is not automatically an incident**: expected trade frequency is strategy/regime-dependent. Flag unusual no-trade/no-fill only when market is active and the strategy is expected to quote.

## 5. Reconciliation and safe intervention (OM-1 and OM-2)

### Authoritative reconciliation

At startup, after reconnect/token refresh, after any order timeout/ambiguous response, after fills, and periodically:
1. Gate new risk-increasing orders.
2. Fetch authoritative scoped open orders, fills, positions, balance/equity, and exchange metadata.
3. Dedupe/replay missed events and reconstruct local order state.
4. Compare local and venue states with exact instrument/account/tournament scoping.
5. Unlock trading only if feeds and account state are fresh and reconciled; otherwise remain gated and alert.

Use durable client order IDs/idempotency keys, with a bounded replay/retention policy. Account for partial fills and cancel requests that arrive after a fill. Unknown order state must block potentially duplicative placements.

### Circuit-breaker transitions

Target state diagram (the merged generic supervisor currently implements only `PAUSED`, `ACTIVE`, `STOPPED`; `STARTUP`, `RECONCILING`, and `DEGRADED` are not enum states):

~~~
STARTUP -> RECONCILING -> ACTIVE
                |            |
                v            v
             PAUSED <---- DEGRADED
                |
                v
            STOPPED
~~~

- **PAUSED**: no risk-increasing orders; controlled cancels and risk-reducing actions allowed only when venue state is sufficiently certain.
- **STOPPED**: terminal local strategy state; does **not** imply exchange-side orders have been cancelled.
- **Kill switch**: stop new intents -> cancel/verify outstanding orders -> fetch authoritative fills/positions -> optionally reduce exposure based on venue and operator policy -> verify outcome. Preserve unknown outcome status if venue is unavailable.
- **Independent watchdog**: separate process/service watches process heartbeat and state reconciliation; no autonomous flatten attempts on unverified state.
- Maintain cancellation request capacity under throttling so ordinary quoting cannot exhaust the recovery budget.

Safety invariants:
- Never trade on stale, unsequenced or unreconciled market/account state.
- Never treat a timed-out order/cancel as definitely unsuccessful.
- Never place replacement orders until cancellation/reconciliation policy permits it.
- No action may exceed max single-order, gross/net, instrument, scenario, leverage or drawdown limits, including resting and pending orders where relevant.
- Risk control failure must fail closed *even if* exporter, DB or notification service is unavailable.

## 6. Perpetual Alpha / Hyperliquid-specific monitors

**Scored venue correction:** The participant guide and merged mainline identify scored DEX **pas** (USDP, Hyperliquid testnet), whereas practice is **par** (USDPR). Older historical references to scored **pa** are stale. All queries, alerts, account checks and identifiers must explicitly carry DEX; validate actual configured profile at launch. Do not treat earlier pa probe results as pas readiness.

Capture:
- DEX-scoped positions, cash/equity (account-mode aware), market capacity (availableToTrade/maxTradeSzs), open orders and fills.
- Mark and oracle prices, funding **estimate versus settled funding**, fee/funding attribution, hourly funding payments and accumulated carry.
- Maintenance margin, available margin, leverage, liquidation distance and gross/notional exposure; monitor across unified/cross-margin collateral where applicable.
- Quote spread, markout/adverse selection, stale quote lifetime, price/size quantization rejects, post-only rejects, throttle budget and available cancellation headroom.
- Funding/oracle-to-mark divergence, invalid/two-sided book checks, and market metadata/drift; portfolio- and symbol-level risk.
- Daily equity returns / volatility and the exact **official scoring formula**, configured separately rather than inferred from a simple P&L chart.

Action examples: large margin consumption -> disallow new exposure; stale book -> pause quoting; material position mismatch -> pause and reconcile; funding outlier -> warn/reassess strategy, not unconditional halt.

## 7. SIG Predictions Cup-specific monitors

SIG is event-contract trading, **not** perps; do not import leverage/liquidation/funding metrics. In the SIG adapter the tradable unit is an **exchange** within a market; internal instrument is SIG_EXCHANGE:<exchange_id>, YES-normalized. The API supports buy-YES bids and buy-NO complement asks; there is **no post-only field**.

Capture:
- Tournament-scoped SUSQie cash, positions, open orders, fills, committed cash and scenario-based terminal wealth distribution.
- Forecast fair values, quoted probabilities, edge at entry, predicted vs realized resolution outcomes, Brier/calibration metrics, and correlated outcome/scenario exposure.
- Book asOf version, revision / previousRevision gaps, resyncRequired, market activity, realtime lag, token expiry and REST resync age.
- Order idempotency / in-flight status; 409 REQUEST_IN_FLIGHT, 502 ORDER_STATUS_UNKNOWN, 429 RATE_LIMITED, 503 cancel-all partial completion; HTTP 200 confirmation before assuming cancel-all complete.
- Per-account **100 reads/min and 30 writes/min** (as documented in the adapter's API-contract notes); preserve budget for reconciliation and cancellation.
- Final settled balance/rank when available; avoid equating interim unrealized marks with final tournament wealth.

SIG realtime is best-effort and one-second batched: REST is authoritative. Resync on connection restoration, revision gap, token refresh, resyncRequired, and periodically, with the minimum call rate consistent with the API limit. Risk limits must account for correlations among contracts (multiple election markets may share the same outcome drivers).

## 8. Dashboards and alerts (OM-3)

### Dashboards

**A. Operations (mobile glance):** bot/trading gate state, heartbeat, feed lag, last reconciliation, WS connectivity, outstanding/unknown orders, error rate, API headroom, active alerts.

**B. Risk & account:** equity/cash, realized + unrealized P&L (separated), drawdown, exposure by symbol/market, outstanding-order exposure, margins/liquidation distance (perps), joint outcome scenario losses (SIG).

**C. Execution & strategy:** order/fill funnel, latency p50/p95/p99, slippage, effective/realized spread, adverse selection markouts, fees/funding, model edge versus realized results, performance by market/model version/regime.

### Alert classification

| Severity | Trigger examples | Action |
| --- | --- | --- |
| Critical / automated gate | state mismatch, missing authoritative state, stale feed beyond calibrated tolerance, risk hard-limit, near-liquidation risk | stop risk-increasing orders; attempt safe cancel/reconcile; page operator |
| High / immediate alert | persistent unknown order status, repeated cancel failures, missed watchdog heartbeat, sustained disconnected data feed, near-exhausted action budget | page; transition to degraded/paused as appropriate |
| Medium / review | elevated rejects/latency, high maker adverse selection, unusual funding/fees, no fills during expected active regime | notify or daily review; strategy adjustments need approval |
| Informational | scheduled deploy, successful recovery, daily P&L/score report | event journal / periodic digest |

**Thresholds are configuration, not guessed facts**: derive stale-feed tolerances from venue cadence; budgets from actual API responses; loss and notional limits from explicit operator risk appetite. Require hysteresis, consecutive-breach windows and deduplicated alerts. Never report an unsuccessful alert delivery as confirmation that risk has been controlled.

### Alert paths

Prometheus -> Alertmanager -> Telegram/mobile notification (or another configured channel). In addition, a process-independent dead-man/heartbeat check must detect a silent bot/monitor host. No API credentials in Telegram messages.

## 9. P&L attribution and strategy research (OM-4)

Record P&L by instrument and strategy run, separating:
- Position mark-to-market movement, realized trading cashflows and unrealized positions.
- Fees, maker/taker execution effects, funding/carry for perps.
- Passive spread capture versus subsequent markout / adverse selection.
- Forecast edge at decision versus execution slippage versus outcome error.
- Inventory/skew penalties and concentration, with relevant horizon.
- Tournament score as a separate authoritative outcome (do not assume rank is available via API).

Build post-trade cohort analyses: by market, liquidity regime, time of day, quote distance, volatility, inventory bucket, model/config version, and position age. SIG forecast calibration is statistical across resolved markets, with caution about correlated outcomes and small samples.

## 10. Delivery plan and exit criteria

### OM-0 — Contracts and instrumentation [P0]
- [x] Define an initial scoped event envelope, exact Decimal serialization and schema versioning (PR #9; target fields in Section 3 remain incomplete).
- [ ] Add correlation IDs to decision/order/fill/risk events and record config SHA.
- [x] Add bounded nonblocking **diagnostic** JSONL journal, backpressure/drop/failure accounting and offline tests (PR #9; not an exporter deployment or durable order ledger).
- [ ] Complete event tests **including event-ID deduplication/replay** (PR #9 tests already cover serialization, redaction, invalid inputs and venue/tournament isolation).

**Exit:** Replayable events and bounded-label metrics contract exist, without adding synchronous external I/O to the order hot path.

### OM-1 — Reconciliation observer [P0]
- [ ] Integrate existing OrderManager lifecycle and venue-specific authoritative query hooks.
- [ ] Rebuild state from startup/reconnect snapshots and replay buffered updates.
- [x] Implement mismatch-detection primitives for fill IDs/coverage, unknown orders, cash and positions (PR #9); PAS runner separately checks SQLite fill/position and orders (PR #11).
- [ ] Implement idempotent recovery from ambiguous place/cancel responses.

**Exit:** Deterministic tests demonstrate recovery across duplicate/out-of-order fills, partial fills, cancel races and unknown timeouts; no unverified state resumes quoting.

### OM-2 — Risk supervision and independent watchdog [P0]
- [ ] Consume risk-gateway decisions and independently audit exposures.
- [ ] Define ACTIVE/DEGRADED/PAUSED/STOPPED transition policy and operator override permissions.
- [ ] Wire fail-closed trading gate to stale-feed, reconciliation and hard-risk events.
- [ ] Complete safe cancel/verify and **deployed independent** heartbeat/dead-man monitor (PR #9 implements one-shot heartbeat tools and cancel planning; PR #11 implements eligible venue-side dead-man refresh with documented limitations; external scheduling/paging remain absent).
- [ ] Validate restart, feed outage, exporter outage, venue outage and kill-switch behavior.

**Exit:** Fault-injection passes; trading cannot silently continue while account/order state is unknown; dashboard outage never disables pre-trade controls.

### OM-3 — Metrics, dashboards and alerts [P1]
- [ ] Deploy lightweight Prometheus + Grafana + Alertmanager stack (Docker Compose sufficient).
- [ ] Instrument runtime, data freshness, order lifecycle, state integrity, exposure and budgets.
- [ ] Build operations, risk, execution dashboards and mobile escalation.
- [ ] Alert deduplication, threshold configuration, escalation test, storage/retention limits.

**Exit:** Operator can determine venue/strategy state within a minute and receives tested actionable alerts from simulated failures.

### OM-4 — Attribution and competition scoring [P1]
- [ ] P&L event ledger and reproducible per-instrument/per-run attribution.
- [ ] Hyperliquid: settled funding, fees, markout, margin safety, score input snapshots.
- [ ] SIG: outcome scenario exposure, forecast calibration, settlement wealth reconciliation.
- [ ] Join each fill to original decision and contemporaneous market snapshot.

**Exit:** Trade-level and daily P&L reconcile to authoritative venue balances/ledger subject to documented marking, timing and settlement differences.

### OM-5 — Reports and runbooks [P2]
- [ ] Morning health + overnight exposure summary; daily performance/score recap.
- [ ] Complete operator runbooks for all venues/incidents (PAS MVP-0 runbook covers preflight/recovery; rollback, deployed alerts, incident response and SIG runbooks remain incomplete).
- [ ] Maintain signed-off preflight checklist and dry-run recovery evidence.
- [ ] Feed actual operational findings into strategy research and subsequent workstreams.

**Exit:** Unattended run can be operated safely using only dashboard, alerts, and tested runbooks; incident records link to replay evidence.

## 11. Minimum release / safety gate

Before considering an unattended scored bot:
- [ ] Venue profile/account/tournament and trading permissions explicitly confirmed.
- [ ] Wallet/key security hygiene verified outside source control.
- [ ] Order/fill/position/cash reconciliation has been exercised against the venue.
- [ ] Freshness and state uncertainty automatically gate risk-increasing orders.
- [ ] Cancel/verify/recover path tested; no local process exit mistaken for exchange cancellation.
- [ ] Risk limits enforced independently of telemetry stack.
- [ ] Metrics and structured events emitted with timestamp/config provenance.
- [ ] Separate watchdog and mobile alert path tested.
- [ ] A scored venue dry-run/preflight is documented; do not infer scored readiness from practice connectivity.

## 12. Test matrix

| Scenario | Expected invariant |
| --- | --- |
| Feed stops, process stays alive | stale-data gate triggers; no new exposure |
| Cancel response lost | order status unknown; reconcile before replacement |
| Order accepted, ACK lost | dedupe/idempotency; no doubled order |
| Partial fill during cancel | final position and remaining order quantity reconcile |
| WS revision gap / reconnect | snapshot resync before live quote resume |
| DB/Prometheus/Grafana down | trading safety unaffected; telemetry loss counted |
| Bot process crashes with live orders | independent supervisor alerts; exchange open orders are not assumed cancelled |
| SIG API 429 / 502 / 503 | bounded retry preserving idempotency, reconcile unknowns, respect budgets |
| Hyperliquid par/pas mix-up | fail closed before reading/trading wrong account or DEX |
| SIG tournament mix-up | no cross-tournament state, positions or orders |
| Event replay of same fill | P&L/exposure unchanged after dedupe |
| Kill switch with disconnected venue | local intent gate stops; outstanding venue orders remain explicitly **unverified** |

## 13. Implemented source layout versus proposed future work

~~~
observability/
    events.py              # MERGED: scoped/redacted event envelope
    journal.py             # MERGED: async best-effort diagnostic JSONL
    reconciliation.py      # MERGED: scoped snapshot observer
    supervisor.py          # MERGED: standalone gate; NOT PAS runner-wired
    watchdog.py            # MERGED: heartbeat write/check primitives
apps/ops_watchdog.py       # MERGED: one-shot external checker
tests/test_observability_p0.py # MERGED: offline fault and serialization tests
execution/sqlite_journal.py    # MERGED: durable execution WAL, separate
execution/durable_dispatch.py  # MERGED: guarded async send primitive
execution/pas_mvp0_runtime.py  # MERGED: experimental PAS single-process runner

# PROPOSED / NOT IMPLEMENTED:
observability/metrics.py
observability/analytics/{pnl,execution,calibration}.py
deploy/monitoring/         # Prometheus, Grafana, Alertmanager
~~~

Future venue-specific serializers and exchange API calls belong in their adapters.
Do not proliferate infrastructure before integrated OM-0/OM-1/OM-2 controls are verified.

## 14. Review cadence and ownership

- **Continuous:** automated safety and heartbeat/freshness checks.
- **At each fill/order transition:** execution, exposure and decision linkage.
- **Every 1–60 minutes (configurable):** aggregation and risk/strategy trend.
- **Daily:** equity/score inputs, P&L attribution, incident summary, significant anomalies.
- **After deployment/model change:** compare version cohorts; run explicit startup/reconciliation preflight.

## References and status

- Existing implementation: README.md, docs/PERPETUAL_ALPHA.md, execution/order_manager.py, notes/20261015_perpetual_alpha_competition_rules.md (the dated notes contain earlier pa information; use active corrected pas configuration).
- SIG contract notes on sig-predictions-scaffold: docs/SIG_PREDICTIONS.md.
- Merged implementation references: PR #6 (order/risk), PR #9 (OM primitives), PR #10 (SQLite WAL), PR #11 (PAS MVP-0).
- Open SIG mainline adapter: PR #12; no complete SIG monitoring runner is shipped.
- This document combines a **target workstream specification** with verified implementation status. Unchecked tasks remain incomplete; some already have tested primitives but lack real send-path integration, deployment, live fault evidence or SIG coverage.
