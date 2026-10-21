# Operations & Monitoring — P0 MVP (2026-10-21)

**Status:** Implemented primitives + offline tests; **not** wired into continuous trading, SIG live runtime, external alerts or an unattended deployment.  
**Tracking:** [OM workstream #7](https://github.com/kyle-nguyennn/market-maker/issues/7), [full specification PR #8](https://github.com/kyle-nguyennn/market-maker/pull/8).  
**Integration base:** feat/perpetual-alpha-order-risk-quoter / PR #6 (order lifecycle and pretrade risk).  
**Scored Perpetual Alpha DEX:** **pas** / USDP (not pa). Practice remains par / USDPR.

## Delivered

| Work package | MVP implementation | Remaining for production |
| --- | --- | --- |
| OM-0 | EventScope + JSON operational event envelope, exact Decimal encoding, key redaction, config/deploy/correlation fields; bounded AsyncEventJournal + JsonlSink and drop/failure counters | Wire emissions at real adapters/strategy/dispatcher; durable transactional order ledger and log rotation |
| OM-1 | AccountReconciler compares scope, account, positions, cash, complete matching fill coverage, snapshot freshness; delegates open-order verification to existing OrderManager.resynchronize() | Authoritative REST account/fill/order fetchers for par/pas and SIG, missing-fill replay and cursor checkpoints |
| OM-2 | SafetySupervisor PAUSED/ACTIVE/STOPPED, operator re-arm, monotonic feed/heartbeat/reconciliation deadlines, fail-closed pretrade callback, kill-switch observation, cancel planning only | Wire to real send gateway; external notification, controlled reduction, process supervisor |
| Dead-man | Atomic heartbeat writer, external one-shot watchdog CLI with missing/stale/future/scope checks | Have deployed bot write heartbeat, run independent schedule and send alerts |

## Required integration sequence

1. Construct EventScope(venue, environment, account_id, tournament_id) using the authoritative runtime venue profile. Each strategy instance has one scope.
2. Instantiate OrderManager with matching dex/account and feed normalized order events. Persist action reservations and CLOIDs by a **separate durable order-intent ledger** before network dispatch; AsyncEventJournal is diagnostic and may lose events.
3. Build LedgerSnapshot and VenueSnapshot from the same complete fill-history interval and authoritative account/order/fill snapshot. The coverage_id is evidence they refer to the same complete fill interval/cursor. Call AccountReconciler.verify() on startup, reconnect, ambiguous responses and periodically. Do not overwrite local state to mask discrepancies.
4. Construct SafetySupervisor with synchronous, venue-specific pretrade_check (Hyperliquid: risk_manager.evaluate_placement) and risk_killed=lambda: risk_manager.kill_switch. **Call authorize_placement at the real send gateway**, after quantization, immediately before every place-order request. This MVP does not patch the dispatcher.
5. Call on_market_update only for *freshly accepted, validated, sequenced* book updates; on_bot_heartbeat from the running bot; on_reconciled only with actual verification result.
6. Resume only with explicit operator authorization and fresh evidence. On pause, request_safe_cancels returns cancel requests **without executing them or confirming cancellation**. Confirm via authoritative venue state.
7. Monitor AsyncEventJournal drop/failure counters. Prometheus, journal, dashboards, and notifications are not pretrade risk dependencies.

### Example (not a runnable live bot)

~~~python
from observability.events import EventScope
from observability.journal import AsyncEventJournal, JsonlSink
from observability.reconciliation import AccountReconciler
from observability.supervisor import SafetyPolicy, SafetySupervisor

scope = EventScope("hyperliquid", "pas", TEAM_WALLET)
reconciler = AccountReconciler(order_manager, scope)
journal = AsyncEventJournal(JsonlSink("var/ops/events.jsonl"))
supervisor = SafetySupervisor(
    order_manager, scope,
    SafetyPolicy(max_feed_age_s=3, max_heartbeat_age_s=10,
                 max_reconciliation_age_s=30),
    pretrade_check=risk_manager.evaluate_placement,
    risk_killed=lambda: risk_manager.kill_switch,
)
# After live validated feed, bot heartbeat, and authoritative sync:
# result = reconciler.verify(local_ledger, venue_snapshot)
# supervisor.on_reconciled(scope, result)
# supervisor.arm(operator_authorized=True)
# Before each real send: supervisor.authorize_placement(intent, fresh_risk_snapshot)
# journal.close() during shutdown only, never on the hot path
~~~

Thresholds above are examples, **not calibrated production limits**.

### External dead-man check

The bot must periodically call:

~~~python
from observability.watchdog import write_heartbeat
write_heartbeat("/run/market-maker/heartbeat.json", scope)
~~~

An independently scheduled process can run:

~~~bash
python -m apps.ops_watchdog \
  --path /run/market-maker/heartbeat.json \
  --venue hyperliquid --environment pas \
  --account-id "$TEAM_WALLET" --max-age-seconds 30
~~~

Exit 0 means recent, correctly scoped heartbeat. Exit 2 means missing/stale/malformed. Arrange external scheduling and paging on nonzero status; this implementation only reports status, **not** notifications or venue-side cancellation. Protect runtime heartbeat-file permissions.

### Deterministic verification

~~~bash
python -m pytest -q tests/test_observability_p0.py
python -m pytest -q
~~~

These tests do not load credentials or send exchange requests.

## Non-goals / release blockers

- No market placement, cancellation, flattening or leverage calls.
- No actual authenticated pas/par/SIG monitoring adapters wired yet.
- No transactional durable CLOID/order ledger; async telemetry is best-effort.
- No P&L analytics, dashboards, Prometheus exporter, Telegram or continuous dead-man service yet.
- SIG order IDs and venue semantics are not interchangeable with Hyperliquid CLOIDs; share reconciliation *concepts*, not order wire formats.
- **No unattended trading readiness claim** until adapter snapshots, the actual dispatch gateway, order identity durability, process supervision and independent alerting are integrated and fault-tested.

## Follow-up priority

1. OM-1: authoritative scoped read-only snapshot/fill providers from live venue APIs.
2. OM-2: mandatory authorization at the real send path, persistent intent ledger and cancel/unknown recovery tests.
3. OM-0: emit decision/order/fill/risk events from actual bot and wire drop counters.
4. External watchdog schedule and notification integration.
5. OM-3 metrics/alerting, then OM-4 strategy attribution and scoring.

## Acceptance evidence

Offline tests cover Decimal and safe event serialization, bounded journal/backpressure and sink failures, cash/position/fill/order mismatches, unknown ACKs, foreign orders, stale feeds, re-arming, pretrade exceptions, cancellation plans and external dead-man checks. They do **not** validate live exchange reconciliation or safe production operations.
