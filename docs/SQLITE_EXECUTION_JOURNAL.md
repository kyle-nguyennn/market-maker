# Perpetual Alpha: SQLite WAL execution persistence

**Status:** Implemented SQLite journal and asynchronous execution dispatcher, draft PR #10. Not a production trading runner. No live orders, keys, or wallet writes used in testing.

## Decision

The authoritative *local* order-action audit journal uses SQLite WAL with a single dedicated database writer and synchronous=FULL. New placements require a durable SQLite commit before invoking the existing risk-checked SDK gateway. This durability cost is confined to the **outbound execution pipeline**, never the market-data subscription callbacks or strategy/feature calculations.

- Hot book/rolling features: process memory; no persistence on the callback path.
- Durable order actions, fills, safety latch: SQLite WAL, single writer, bounded FIFO.
- Later market data history and research: asynchronously batched Parquet.
- Redis Streams: existing optional *lossy* warm-history proposal, not reliable execution persistence.
- HDF5: unnecessary for timestamped event records here.

Store the database on a stable local/attached disk whose sync and locking guarantees are tested. Ephemeral deployment filesystems can destroy WAL durability across instance replacement. Back up the **SQLite database using SQLite's backup API** or another safe checkpoint procedure; do not copy the main file in isolation while WAL is live.

## Architecture

    MarketDataHandler -> FeatureEngine -> Quoter -> Risk -> OrderManager
                                                      |
                                          submit PlaceAction
                                                      |
                                                      v
                                        Bounded placement executor
                                                      |
                                              prepare_place()
                                                      |
                                                      v
                                         Bounded DB writer queue
                                                      |
                                      SQLite WAL COMMIT (FULL)
                                                      |
                                         durability receipt
                                                      |
                                                      v
                                     SafeExecutionGateway.dispatch
                                   [fresh risk / account / data]
                                                      |
                                          Optional approved SDK send
                                                      |
                                         record_place_outcome()
                                                      |
                                              Future resolves

The submission API immediately returns a concurrent.futures.Future. Only the placement worker waits for SQLite; the caller must **never wait on the Future in a WebSocket callback**. The existing SafeExecutionGateway still refuses live sends unless separately enabled and operator-authorized. The scored pas SDK additionally requires PERPETUAL_ALPHA_PAS_ALLOW_TRADING=true, which remains **false** by default.

### Crash-window invariants

1. Prior to the durable prepare commit: **no send**. If the writer/queue/disk fails, the dispatcher refuses the new order and quarantines OrderManager.
2. After PREPARED but before SDK call: on restart, treat it as **possibly sent**, not necessarily unsent; a crash can occur on either side of the network boundary.
3. After SDK call but before outcome commit: same conservative PREPARED/unknown recovery.
4. SENT records actual SDK dispatch, but do **not** establish current resting/fill/cancel status.
5. NOT_SENT records that the gateway definitively rejected the action before contacting SDK.
6. A repeated place action key (same CLOID) is a conflict. Never generate a replacement CLOID and blindly retry an ambiguous action.
7. A venue ACK for cancellation is nonterminal; the exchange order/position state must still be confirmed.

At restart, call load_recovery() and inspect all PREPARED/SENT placements, verify venue/account identity, query REST open orders and order-status by CLOID/OID, replay uniquely identified fills and positions, validate funding/USDP/margin state, then resynchronize OrderManager. **This restart coordinator is not yet implemented**: the journal provides the durable evidence, but does not make a full trading system safe to start.

### Urgent cancellation behavior

Cancellations use a separate bounded priority worker. They dispatch through SafeExecutionGateway **before** the journal audit, so SQLite failure does not stop a risk-reducing cancel. An audit failure flags the manager for recovery and reports an exception even if the venue was contacted. Earlier durable place records remain for later reconciliation. SDK access between internal workers is serialized by a lock; **other producers must still serialize all non-thread-safe OrderManager mutations**. This is an integration primitive, not a multi-thread-safe full runner.

## SQLite schema

- journal_identity: schema version + fixed DEX/team address; opening a mismatched database fails closed.
- journal_actions: place/CLOID unique identity, exact text Decimal price/size, PREPARED/SENT/NOT_SENT status, audit outcomes and cancellation attempts.
- journal_fills: venue fill-ID primary key, exact text Decimal size/price/fee, timestamps; duplicate with same payload is no-op, conflicting duplicate is rejected.
- journal_safety: persistent one-way kill-switch latch and reason.

No API wallet keys/secrets or raw untrusted SDK payloads are stored.

Separate database per wallet + DEX is recommended, e.g. data/state/pas-trading.sqlite. The implementation rejects in-memory databases to prevent accidental disappearance after restart. The local filesystem path is ignored by git.

## Public API and example

    from execution.sqlite_journal import SQLiteWALJournal
    from execution.durable_dispatch import DurableExecutionDispatcher

    # Existing gateway constructed with live_enabled=False by default.
    with SQLiteWALJournal(
        "data/state/pas-trading.sqlite",
        dex="pas", account_id=team_wallet,
    ) as journal:
        recovery = journal.load_recovery().result()
        # Validate recovery.kill_switch and reconcile recovery.unresolved
        # against authoritative venue state BEFORE permitting new placements.
        with DurableExecutionDispatcher(
            gateway=gateway, journal=journal
        ) as dispatcher:
            future = dispatcher.submit(action, fresh_risk_snapshot)
            # Await future only on execution/control thread, not the WS handler.
            result = future.result()

Explicit idle-time maintenance is available via journal.checkpoint() or
journal.checkpoint(truncate=True), returning SQLite's busy/WAL-page/checkpointed
counters as a Future. Do not wait for it in a market-data callback.

Additional methods:
- prepare_place(action) => Future[JournalReceipt], commits before resolution.
- record_place_outcome(receipt, sent, reason) => Future[None].
- record_cancel(action, sent, reason) => Future[int] (best-effort post-send audit).
- record_fill(fill_id, client_order_id, instrument, size, price, fee, event_ts_ms) => Future[bool] (idempotent).
- latch_kill_switch(reason) => Future[None] (no unreviewed reset method).
- load_recovery() => Future[RecoverySnapshot]; close() drains accepted tasks during normal shutdown.

## Operations, benchmarks, outstanding gates

Configure PRAGMA journal_mode=WAL, synchronous=FULL, wal_autocheckpoint=0 and bounded busy_timeout. Control-thread initialization and graceful shutdown may block; **not the data hot path**. Plan manual/on-idle checkpoints, WAL size alerts, disk-space and backup tests. A disk full or queue saturation must stop **new placements**, not emergency cancellation attempts.

Benchmark p50/p95/p99 queue wait, WAL transaction durability latency, quote-to-ACK, startup replay time, and WAL growth on actual deployment storage. Run controlled power-loss/process-kill fault tests.

**Unimplemented and still required before unattended trading:** verified restart orchestration, durable CLOID session policy, WS order/fill event normalization, single-owner order-manager coordination, account equity/capacity normalization in unified mode, rate budgets, risk limits, and explicit human permission to enable PAS signed trading. PR #10 neither changes the real API account nor places test orders.
