# Market-data architecture: hot state, rolling features, and Redis warm history

**Status:** Design proposal — not implemented  
**Date:** 2026-10-16  
**Scope:** Read-only Hyperliquid `par` / `pa` market feeds and future venue-independent market-data producers.  
**Branch:** `feat/perpetual-alpha-order-risk-quoter`  
**Related code:** `market_handler/hyperliquid_market_data_handler.py`, `market_handler/hyperliquid_adapter.py`, `model/market.py`  
**Non-goal:** Change order execution, risk authorization, existing book correctness, or start live trading.

## 1. Decision

Keep **`HyperliquidMarketDataHandler`** focused on receiving, validating, sequencing, and maintaining the **current actionable** immutable `MarketState` for one instrument. Delegate *accepted observations* to a separate **`MarketDataHistory`** component using an **O(1)-expected, nonblocking, bounded `offer()`** call.

`MarketDataHistory` owns (a) a bounded in-process buffer to absorb differing upstream and Redis drain rates, (b) **keyed conflation of pending, *not-yet-persisted* state snapshots under backpressure**, and (c) an asynchronous Redis writer. The handler does not know Redis commands, stream schemas, retry policy, or storage availability.

Redis is a **warm, lossy rolling historical cache**, not an exchange book of record, a replacement for the hot in-process book, or a guaranteed audit journal. Optional Parquet archival is a later, independent sink.

### Data-flow diagram

```text
                  Hyperliquid REST / WebSocket
                               |
                               v
                 HyperliquidMarketDataHandler
                  normalized accepted MarketState
                  + feed_epoch + acceptance_seq
                               |
              +----------------+----------------+
              |                                 |
              v                                 v
     Live ordered observations       MarketDataHistory.offer()
              |                        (nonblocking/history only)
              v                                 |
         FeatureEngine                           v
       bounded rolling windows             bounded FIFO
       incremental features                + keyed conflation
              |                                 |
              v                                 v
        FeatureSnapshot                  async Redis writer
              |                                 |
              v                                 v
     QuotingContext assembler               Redis Streams
      (MarketState version                  (warm, lossy cache)
       + Features same version                    |
       + PortfolioSnapshot)                       v
              |                         offline readers / optional
              v                          archival / feature warmup*
            Quoter
              |
              v
      Risk -> OrderManager -> Gateway

* Warmup may use persisted history only if its completeness, ordering,
  epoch and provenance can be validated. Live signals never depend on Redis.
```

**Critical invariant:** Redis availability, Redis write latency, and historical buffer congestion **must never hold the market handler's state lock or block the SDK WebSocket callback**. A Redis outage may degrade historical completeness but may not turn fresh data into an old actionable book. Conversely, quoting must continue to honor the market handler's own freshness and data-quality gates independently of history.

## 2. Responsibility boundaries

| Component | Owns | Must not own |
| --- | --- | --- |
| `HyperliquidMarketDataHandler` | Latest accepted full L2 book, DEX/instrument scope, WS callback, REST bootstrap, ordering, freshness, validity, subscription lifecycle | Historical ring, Redis client/writer, Redis retries, historical conflation |
| `HyperliquidMarketAdapter` | Hyperliquid wire -> venue-neutral Decimal `MarketState` | Buffers, connections, persistence |
| `FeatureEngine` (new) | Ordered, in-process rolling statistics and versioned feature snapshots; gap detection and warmup | Redis I/O, venue SDK dependencies, quoting policy, positions/risk | 
| `Quoter` (existing, interface change proposed) | Strategy policy given current book + aligned features + portfolio; decides fair price, spreads and sizes | Historical ingestion, Redis buffering, raw wire parsing | 
| `MarketDataHistory` (new) | Asynchronous ingestion API, buffer capacity, overflow policy, per-key conflation and loss tracking, writer/sink lifecycle | Live order-book authority, risk gating, SDK subscriptions |
| `RedisMarketDataSink` (new) | Stream encoding, pipelined `XADD`, bounded retention, short network timeouts, transient-error handling | Live feed callback, strategy evaluation, order/risk state |
| `MarketDataHistoryReader` (future) | Time-range warm-history lookup and gap metadata | Claim of complete tick replay from a lossy cache |

`MarketDataHistory` should be **venue-neutral**. It accepts internal Decimal-based market observations and metadata, never a Hyperliquid SDK object. The **handler owns the history dependency** (optional injection of a minimal interface), while buffering and Redis-push mechanics remain inside `MarketDataHistory`.

Proposed contract (illustrative, **not code implemented**):

```python
class MarketDataHistory(Protocol):
    def offer(self, observation: MarketObservation) -> OfferResult: ...
    def stats(self) -> HistoryStats: ...
    def flush(self, timeout_s: float) -> FlushResult: ...
    def close(self, timeout_s: float) -> None: ...
```

`offer()` must never wait for Redis, a worker wake-up, or free queue capacity. It should use a bounded short critical section and signal overflow instead of blocking the WebSocket thread; if even the history lock cannot be acquired immediately, drop **only the history publication** and record a loss counter. `OfferResult` should distinguish `ENQUEUED` / `CONFLATED` / `DROPPED` / `DISABLED`, for metrics rather than for market-data acceptance.

A handler should call `offer()` **only for genuinely accepted book updates**, not when `apply_snapshot()` returns an existing state after rejecting an old REST/WS frame. Give each accepted update a monotonically increasing per-handler **acceptance sequence** under the same market-state lock, then hand the immutable observation off outside that lock; publication must not permit an earlier sequence to overwrite a newer pending conflation key. If keeping the acceptance and offer operations separate, support sequenced offers with out-of-order reconciliation.

## 3. Market observation schema and conflation keys

The payload is **the normalized immutable snapshot** (`MarketState`) plus transport/quality metadata, not a mutable bid/ask dictionary.

Suggested fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Wire serialization version, initially 1 |
| `venue` / `dex` / `instrument` | E.g. `hyperliquid` / `pa` / `BTC`; preserve DEX isolation |
| `channel` | `l2`, `bbo`, `trades`, `asset_context` |
| `exchange_ts_ms` | Exchange timestamp when provided; **not** an arrival-order guarantee |
| `received_ts_ns` | Monotonic or wall-clock ingest timestamp, with clock type explicitly labeled |
| `ingest_seq` / `feed_epoch` | Per-producer acceptance sequence and reconnect generation |
| `source` | `ws` / `rest` |
| `is_valid` / `invalid_reason` | State-quality annotation; invalidity must not become invisible |
| `payload` | Decimal values serialized as lossless decimal strings |
| `history_gap` / `conflated_count` | Whether intermediate states were skipped and how many, plus skipped time range if known |

**Conflation key** = `(venue, dex, instrument, channel, optional partition)`. For full L2, use `(hyperliquid, pa, BTC, l2)`. **Do not use price level as the key**: Hyperliquid `l2Book` frames are *complete* snapshots and do not require applying deltas to reconstruct a book.

**Publish only the latest accepted observation per key during overload**, determined by producer sequence/epoch (and validated exchange time), not by an arbitrary Redis completion order. Preserve distinct `par:BTC` and `pa:BTC` histories.

### Conflation eligibility

| Event family | May conflate pending events? | Reason / policy |
| --- | --- | --- |
| Complete L2 book snapshot | **Yes** | Newer full snapshot supersedes older pending snapshots for *current-state* history, but reduces replay fidelity |
| BBO snapshot | **Yes** | Newer BBO supersedes older pending BBO; lost microstructure transitions must be flagged |
| Mark/oracle/funding **state snapshot** | **Yes** | Latest state supersedes prior pending state; do not treat actual funding settlement as a state snapshot |
| Individual trade prints | **No** | Every trade carries potentially unique size/side/id; bounded queue overflow creates an explicit gap, not invented aggregation |
| Order/fill/cancel/risk/audit events | **Never in this component** | Must use a separate reliable journal; silently losing one can break position/risk accounting |
| Actual funding settlement events | **No** | Cash flow cannot be conflated as a mere quote update |

An optional **explicitly named lossy trade aggregate** (e.g. volume/imbalance per fixed window) could be introduced later, but it must never be presented as a sequence of original trades.

## 4. Buffering and degraded-mode algorithm

Target: preserve all accepted snapshots **when the sink keeps up**; switch to bounded **latest-per-key conflation** only when in-memory pressure or sink congestion requires it.

### Normal mode

1. Handler commits a valid or explicitly invalid full book observation to hot state; read-only history publication is independent.
2. `offer()` appends an immutable observation to a bounded MPSC ingress FIFO, recording byte and entry counts. The hot path returns immediately.
3. One background writer drains in batches to Redis, preserving ordering *within each conflation key*. It reports acknowledged writes. Redis commands execute **only** on this worker.
4. Use a separate configurable soft watermark (`high_watermark`) to enter degraded mode *before* exhausting memory. No sleeping/retrying in `offer()`.

### Congestion / Redis-sink-full mode

Treat Redis timeout, connection failure, OOM/maxmemory write rejection, persistent high write latency, or a full local ingress queue as **backpressure**. Redis stream trimming under normal bounded retention is *not* backpressure.

1. Enter a **latched overflow epoch** for the impacted sink. Preserve already-queued FIFO items ahead of overflow; do not interleave newly accepted full snapshots back into the FIFO until the old backlog and keyed overflow drain.
2. Route **conflatable state observations** to a **bounded keyed pending map**, one latest unsent observation per `(venue,dex,instrument,channel)`. On a second observation for the same key, replace its pending value if and only if it is newer (producer sequence/epoch). Increment `conflated_count` and record the lost intermediate exchange-time interval.
3. If the keyed map is at its **configured entry/byte limit** with a new key, evict the oldest *conflatable* pending entry under a documented fairness policy and record a per-key loss/gap marker. Do **not** discard the newest snapshot of one active market indefinitely merely because another market is busier; track per-key quotas/age/fairness.
4. Nonconflatable trade observations use their own bounded FIFO/reserve. If full, apply an explicit configurable drop policy (proposed: drop newest), increment `dropped_trades`, and publish an eventual gap marker. Never relabel dropped trades as a complete trade tape.
5. Worker drains in two phases: **older primary FIFO first**, then keyed pending states in producer order/fair schedule, maintaining per-key monotonicity. Keep overflow mode latched while outstanding keyed states or writes remain; newer data for those keys continues to coalesce instead of bypassing pending older data.
6. On reconnect, retry failed Redis batches with bounded exponential backoff and jitter in the worker; preserve in-flight entries until acknowledgment. The successful write of a consolidated snapshot must include `history_gap=true` and counts/range for skipped observations.
7. Exit overflow only when backlog falls below the low watermark **and** the keyed map is empty. Avoid high/low watermark oscillation.

**Backpressure is never infinite buffering.** Both FIFO and keyed map have strict configurable **entries and bytes** budgets. A high cardinality of symbols/channels must not defeat the memory bound. Under sustained failure, latest-only semantics apply where safe, with conspicuous loss metrics.

**Important Redis nuance:** `XADD` creates an append-only history entry; it does not update a historical stream record in place. **Keyed conflation happens in MarketDataHistory *before* Redis `XADD`**, not via stream overwrite. A Redis OOM response does not imply that a previous write failed cleanly; ambiguous acknowledgment is handled as at-least-once delivery with a stable logical observation ID and deduplicating readers.

### Atomicity and lifecycle

- Worker and `offer()` coordinate via a minimal bounded lock/nonblocking producer technique; never acquire the market-handler state lock from the Redis worker.
- Sequence + feed epoch must identify acceptance order. An older delayed callback cannot replace a newer pending keyed snapshot.
- `start()`/`stop()` of the handler do not implicitly destroy buffered history; `MarketDataHistory` has an independently owned lifecycle. On shutdown, attempt a **bounded** flush, expose remaining unsent count, then terminate without indefinite waiting.
- Invalid/missing/crossed book transitions should remain observable: preserve a distinct **quality-transition/gap marker** or out-of-band health telemetry rather than silently conflating away evidence that a live book went invalid. Do not let history revalidate the live book.

## 5. Redis storage model (proposed)

**Redis Streams** are the initial warm-history sink; keys partition by DEX, instrument and channel:

```text
md:v1:pa:BTC:l2
md:v1:pa:BTC:bbo
md:v1:pa:BTC:trades
md:v1:pa:BTC:asset_context

md:v1:par:BTC:l2  # distinct from pa:BTC
```

For each published observation, worker executes **`XADD`** containing serialized payload and metadata: `schema_version`, `exchange_ts_ms`, `received_ts_ns`, `ingest_seq`, `feed_epoch`, `source`, `history_gap`, `conflated_count`, `payload`. Redis stream IDs establish **sink append order**, not exchange event time. Consumers must read both.

- **Retention:** Configure per-channel `XADD MAXLEN ~ N` or time-based `MINID` (where supported), with an explicit Redis memory ceiling. `MAXLEN ~` is approximate, so verify actual memory consumption. Expiry for idle keys is optional and must not be mistaken for exact per-entry retention.
- **Sizing:** Start with a measured memory budget, not a presumption that every full-book frame is cheap. Record serialized bytes, ingress rate, Redis bytes per stream, and drain throughput. BBO/trades generally need longer horizons than sampled full depth.
- **Encoding:** Choose a versioned compact encoding (e.g. JSON with lossless Decimal strings initially). Compression only if CPU cost proves worthwhile. Preserve the selected DEX and instrument in *both* key and payload.
- **Delivery semantics:** Best-effort, at-least-once for retried writes; not exactly-once. Include stable `observation_id = (producer_session, feed_epoch, ingest_seq)` to allow deduplication of an ambiguous `XADD` retry. Do not use a naive exchange millisecond timestamp alone as a globally unique stream ID.
- **Optional future archive:** Parquet via a separate reader/archiver; a lossy Redis cache cannot retroactively create a complete market replay. If exact event-level replay is needed, add an independent lossless capture route with its own durability guarantees.

## 6. Failure matrix

| Failure | Hot market state | History behavior | Visibility |
| --- | --- | --- | --- |
| Redis temporarily slow | Unaffected | FIFO absorbs rate mismatch; enter keyed overflow at watermark | Queue depth, p99 write latency |
| Redis disconnected | Unaffected | Bounded retry, keyed snapshots coalesce; trades eventually explicitly drop | Sink unavailable, gaps, dropped/conflated counts |
| Redis maxmemory / OOM error | Unaffected | Treat as congested sink; no unbounded in-memory growth | Rejected writes and cache capacity alert |
| History buffer at hard limit | Unaffected | Latest-per-key overwrite/evict where safe; nonconflatable drop policy | Per-key gap marker and overflow counter |
| WS sends older full snapshot | Handler retains newer book | **No** history publication for rejected stale frame | Out-of-order feed counter |
| Book crossed/stale/reconnecting | Handler invalid / quoting should fail closed | Record quality transition or marked invalid observation; never mark complete | Feed-quality and gap metrics |
| Process crashes before Redis ACK | Live handler restarts/reconciles separately | Unflushed in-memory history lost; cache has replay gaps | New session ID and restart/gap marker |
| Ambiguous XADD timeout | Unaffected | Retry with stable observation ID; readers deduplicate | Duplicate-write/retry metric |

**Trading safety:** If a strategy *requires* historical features, it must independently check minimum usable lookback length and gap/freshness status and decline that feature or fail closed; it must never assume Redis history is complete simply because Redis is reachable. Risk and order accounting must never derive authority from this lossy stream.

## 7. Configuration and observability

All knobs should be validated and configurable, per channel where appropriate:

- `history_enabled`; sink mode (`none` / `redis`); Redis endpoint from environment/secret store (no credentials checked into Git).
- Ingress `max_entries` and `max_bytes`, keyed overflow `max_keys` and `max_bytes`, high/low watermarks, optional trade reserve.
- Writer batch size, max outstanding bytes, per-command timeout, retry/backoff ceiling, circuit-breaker reopen delay.
- Redis key prefix, per-channel max stream length / time retention, optional TTL, per-symbol fairness policy.
- Counters: accepted/enqueued/written/conflated/dropped (by venue/dex/symbol/channel/reason), oldest pending age, sink outages, malformed feed, gaps emitted; gauges: buffer occupancy in bytes/entries, distinct conflation keys, Redis latency, oldest undrained observation.
- A **history integrity indicator** (`COMPLETE` / `CONFLATED` / `GAPPED` / `UNAVAILABLE`) for every requested replay interval. `COMPLETE` is only true within the coverage actually captured during a healthy session; existing Redis records alone do not prove completeness.

## 8. Implementation sequence and acceptance tests

**This document records design only. None of the following is currently implemented.**

1. Define venue-neutral `MarketObservation` and a narrow injectable history contract; introduce a no-op implementation for when history is disabled. Keep the market-handler state lock out of Redis operations.
2. Implement the bounded FIFO + keyed overflow `MarketDataHistory` with deterministic thread-safe tests (no Redis dependency).
3. Implement a mockable `RedisMarketDataSink` with stream serialization, measured retention, bounded retries, and stable logical observation IDs; integrate via background worker.
4. Connect **accepted** `HyperliquidMarketDataHandler` updates to `history.offer()` without changing `.bids`, `.asks`, `.current()`, SDK subscriptions, or quote/risk semantics.
5. Test end-to-end in offline/mock Redis, then optional read-only/testnet public-feed soak testing. Do not send authenticated orders.

Minimum deterministic cases:

- Healthy fast sink: all consecutive L2 frames retained in order, no conflation.
- Sink blocked, >buffer capacity: for each L2 key, eventual Redis write contains the **newest** accepted state and explicit skipped-count/time range; total resident bytes remain bounded.
- Interleaved `pa:BTC`, `par:BTC`, and `pa:ETH` cannot overwrite each other; fair drain under skewed rates.
- Old REST or WS frame rejected by handler: no history write. Equal-timestamp acceptance uses handler-assigned sequence rather than timestamps for conflation.
- Malformed WS/crossed-book transition: no falsely healthy history; invalidity becomes visible even if subsequent frames conflate.
- Full trade buffer: explicit loss event; trades never silently conflated; order/fill events never routed to this lossy cache.
- Blocked Redis worker never blocks handler `current()`, `apply_snapshot()`, or the WS callback. Exercise slow Redis, retry, OOM, process stop and restart.
- Redis ACK lost after successful append: retry may duplicate physical stream entries; the history reader deduplicates by `observation_id`.
- After pressure falls below the low watermark, original full-history behavior resumes and future observations retain normal order.

## 9. Explicit out-of-scope choices

- No Redis write or read in the **synchronous** quoting/risk path.
- No claim that `Redis Streams` plus conflation supports exact market-data replay.
- No private execution, order, fill, position, or PnL journal in `MarketDataHistory`.
- No persistence guarantees for the in-memory overflow queue during crashes.
- No automatic switch to live trading, no changes to Hyperliquid API keys, no new signed venue calls.

**Next decision before coding:** select measurable memory/latency targets and whether complete L2 is retained at every healthy WS frame or intentionally downsampled for Redis cost. **Conflation-on-pressure and intentional sampling are different policies** and must be reported separately.


---

## 10. Live feature computation and quoter interface (design, not implemented)

**Decision, 2026-10-16:** Keep the quoter **stateless with respect to market-data history**, use a **stateful, venue-neutral `FeatureEngine`** to compute rolling indicators in memory, and give the quoter **both the entire latest immutable `MarketState` and a version-aligned `FeatureSnapshot`**. The strategy may inspect raw L2 depth for quoting, but it must not independently maintain redundant historical book windows. `MarketDataHistory` remains the separate, possibly lossy Redis warm-history pipeline described above.

### 10.1 Responsibilities and data path

| Concern | Owner | Examples |
| --- | --- | --- |
| Validate market-data payload and maintain current L2 | `HyperliquidMarketDataHandler` / adapter | Book, best bid/ask, validity, timestamps, feed epoch |
| Stateful historical signal inputs | `FeatureEngine` | Recent mids/returns, previous best bid/ask and sizes, bounded rolling depth summaries |
| Derived *descriptive features* | `FeatureEngine` | Realized volatility, top-level/L5 imbalance, microprice, order-flow imbalance |
| Short-horizon directional forecasts (optional) | Separate `AlphaModel` consuming `FeatureSnapshot` | Predictive drift/fair-value adjustment, calibrated horizon |
| Action policy | `Quoter` | Fair value, reservation price, spread, inventory skew, order quantities, quote hysteresis |
| Portfolio and risk | Portfolio snapshot and `RiskManager` | Available inventory, position/exposure constraints, new-order authorization |
| Redis warm-history storage | `MarketDataHistory` and `RedisMarketDataSink` | Bounded buffering, keyed conflation, XADD, incomplete replay markers |

The live path consumes the **accepted, ordered feed directly**; it must **not** query Redis on each quote. The handler delegates to `MarketDataHistory.offer()` for persistence independently of feature computation. A composition/orchestration layer wires accepted snapshots into the feature pipeline, assembles the version-consistent quoting input and handles the scheduling of quote evaluation. No Redis detail leaks into the quoter.

### 10.2 Proposed domain contracts

These signatures are **illustrative**, deliberately not implemented in this change. All calculations affecting prices, sizes and monetary values retain venue-neutral `Decimal` representation. A feature engine may use optimized numeric intermediates only with an explicitly validated precision and determinism policy.

```python
@dataclass(frozen=True)
class MarketObservation:
    market: MarketState
    venue: str
    dex: str
    feed_epoch: int
    acceptance_seq: int
    received_ts_ns: int

@dataclass(frozen=True)
class FeatureSnapshot:
    instrument: str
    dex: str
    feed_epoch: int
    acceptance_seq: int  # exact input book version
    calculated_ts_ns: int
    is_ready: bool
    invalid_reason: str | None
    realized_volatility: Decimal | None
    l1_imbalance: Decimal | None
    microprice: Decimal | None
    order_flow_imbalance: Decimal | None
    # Future: L5 imbalance, trade imbalance, model outputs.

class FeatureEngine(Protocol):
    def accept(self, observation: MarketObservation) -> FeatureSnapshot: ...
    def current(self) -> FeatureSnapshot | None: ...
    def invalidate(self, reason: str) -> None: ...

@dataclass(frozen=True)
class QuotingContext:
    market: MarketState
    features: FeatureSnapshot
    portfolio: PortfolioSnapshot
    # Also carries feed_epoch + acceptance_seq for version assertions.

class BaseQuoter(ABC):
    @abstractmethod
    def quote(self, context: QuotingContext) -> list[Quote]: ...
```

**Compatibility note:** The existing `quoter/base_quoter.py` currently defines `quote(state: MarketState)` and its helper methods operate on `MarketState` alone. Changing to `QuotingContext` is a **future interface migration**; it should happen with adapters/tests for existing subclasses, not by introducing a second incompatible default interface. The proposed `PortfolioSnapshot` is a venue-neutral, consistent snapshot, not a raw SDK account response.

A feature must describe a measured property; a directional **signal/alpha** that interprets features should be distinct from the calculation of those features. An alpha model can be injected into a strategy or composed upstream without making generic features strategy-specific.

### 10.3 First four features for Perpetual Alpha

Initial development should favor a small deterministic pipeline, retaining only the minimum observations required:

1. **Rolling realized volatility:** Computed from consecutive mid-price log returns over an explicitly sized event/time window. Specify aggregation clock, duration, annualization (if any), zero/duplicate exchange timestamps and EWMA/rolling-window semantics. For market making the horizon must match quote lifetime or short-term forecast horizon; do not label uncalibrated variance as an annualized `sigma`.
2. **Top-of-book imbalance:** Given best bid size `q_b` and ask size `q_a`, `(q_b - q_a) / (q_b + q_a)` when both are positive and the book is valid. L5 cumulative depth imbalance is an optional separate feature.
3. **Microprice:** Given best bid `b`, ask `a`, sizes `q_b`/`q_a`, compute `(a*q_b + b*q_a)/(q_b + q_a)`. Explicitly reject zero denominator or invalid book; do not confuse a microprice heuristic with a verified execution price.
4. **Recent order-flow imbalance (OFI):** Compare consecutive best-price/size states, using a documented L1 event-based definition, then aggregate over a bounded window. Unlike static depth imbalance, **OFI requires continuity of observations**. A conflated or missing update can materially distort it.

All formulas must be validated against hand-computed fixtures. Features should publish `None` / `is_ready=false` rather than invented fallback values when their required window is absent, invalid or discontinuous.

### 10.4 Memory, update cadence, and version consistency

- Maintain **bounded in-process** rolling deques or fixed-size ring buffers *inside `FeatureEngine`*, one partition per `(venue, dex, instrument)`. Store just the needed observations (mid, returns, best-level sizes, prior-book comparators, timestamps, feed version); full-depth history is not required for all four initial features.
- Prefer incremental updates with bounded work per accepted observation; avoid rescanning Redis or a full order-book history on every quote. Deep-book features may examine the *latest* book directly and cache their result by acceptance version.
- Assign `feed_epoch` and strictly monotonic `acceptance_seq` when the handler **commits** a genuinely new update. Duplicate/older REST observations that the handler rejects must not advance the feature window.
- Publish a `FeatureSnapshot` tagged with the exact accepted book version used to calculate it. The quoter input assembler must compare `(venue, dex, instrument, feed_epoch, acceptance_seq)` before combining `MarketState` and `FeatureSnapshot`. A time difference alone is **not** sufficient proof of alignment.
- Prefer a single ordered per-instrument processing lane with a small, deterministic update budget and atomic publication of the matching `(MarketState, FeatureSnapshot)` pair. Do not run expensive feature calculations while holding the SDK/market-handler state lock. If using an asynchronous feature worker, define its scheduling and overflow policy explicitly and never allow a stale feature snapshot to be paired with a newer book.
- Separate **feature update cadence** (every accepted observation, where path dependence requires it) from **quote decision cadence** (possibly throttled/hysteretic to respect venue request budgets). Recalculating features does not imply cancel-and-replace on every book tick.
- Exchange `l2Book` has no sequence ID, so acceptance sequence describes **local processing order**; it cannot certify complete venue delivery. Mark feed gaps/reconnects separately.

### 10.5 Path-dependent features versus Redis conflation

The history writer's **keyed latest-value conflation** is acceptable for its declared lossy warm-cache purpose. **It must not be applied silently to the live `FeatureEngine` input**, which depends on the full locally accepted sequence.

Example: if the mid moves `100 → 101 → 99`, coalescing directly to `100 → 99` changes realized return variation and order-flow imbalance. Although the newest book is still valid for immediate quoting, the resulting historical features would be inaccurate.

Policy:

1. While the live feature processing lane keeps up, process all **accepted** book observations in their acceptance order. Redis may independently store fewer snapshots.
2. If feature processing falls behind, **do not** silently adopt Redis's most-recent-per-key policy. A bounded feature queue may overflow, but this is a **feature-integrity failure**: record a gap, invalidate continuity-dependent features, and rewarm them from a trustworthy uninterrupted sequence.
3. Stateless or current-book-only features (e.g. imbalance/microprice) may recover on the next valid snapshot; realized volatility/OFI must satisfy their documented rewarm/continuity requirements.
4. When the feed reconnects, restarts, changes epoch, or quality degrades, reset or invalidate affected rolling features. Historical Redis values may help warm a feature only if the requested lookback is verified complete, correctly ordered, source-consistent and not marked `CONFLATED`/`GAPPED`.
5. Keep independent telemetry: `feature_window_ready`, `feature_lag_ms`, `feature_latest_seq`, `feature_gaps`, `feature_reset_reason`, and `history_conflations`. Never conflate the two types of data loss.

### 10.6 Quoting failure behavior and risk boundaries

- The quoter receives the **full latest `MarketState`**, not only scalar features, so it can choose a passive level based on best bid/ask, spread, and book depth.
- When an essential feature is missing, stale, mismatched or invalid, the default conservative strategy **does not generate new orders**. Optional feature-specific fallbacks must be explicit, named and unit-tested; no silent substitution of stale volatility or OFI.
- Feature validity does not supersede **live book** validity, account/capacity checks, inventory constraints or final adapter-quantized pre-send risk evaluation.
- Redis writer outages alone must **not** stop the feature engine or invalidate an otherwise healthy live book, unless the strategy explicitly requires verified persisted history.
- For example, quoter can derive fair value from current microprice/mark and alpha (if available), adjust reservation price by inventory, choose half-spread using volatility and costs, and choose quantities from limits/depth. This remains a strategy policy, **not** part of `FeatureEngine`.

### 10.7 Acceptance tests (proposed)

- Exact hand-calculated fixture results for mid returns/realized volatility, imbalance, microprice and OFI, including empty/crossed/depth-zero inputs.
- Streaming feature updates produce the same values as deterministic ordered batch replay from the same **complete** sequence.
- `FeatureEngine` receives each accepted book once; rejected old REST frames and delayed old-generation WS callbacks do not update windows.
- Feature snapshot `acceptance_seq` and `feed_epoch` must match the market snapshot in a `QuotingContext`; mismatches block quoting.
- Slow/disconnected Redis causes keyed history conflation **without changing** the live feature series or quote results for the same complete accepted market input.
- Simulated feature queue overflow or WS discontinuity makes path-dependent features invalid until rewarm; no invented OFI/volatility continuity.
- Multiple instruments/DEXs and restarts cannot share or contaminate feature rings, version identifiers or Redis keys.
- Quote hysteresis means the features can update many times while the quoter chooses no replacement, preserving request budgets.
- Existing mocked gateway/risk rejection remains the final authority after quoting, including stale book and missing exposure inputs.

### 10.8 Scope and implementation order

**Nothing in section 10 is implemented by documenting it.** First add typed `MarketObservation` and versioned feature/quoting contexts, then implement pure bounded `FeatureEngine` with unit tests, then compose the handler → feature pipeline → quoter in a **paper-only** runner. Integrate Redis history independently so its throughput/backpressure cannot affect quote correctness. Finally prove feed-quality recovery, risk-gateway checks and deterministic replay before considering any authenticated continuous execution.

**Do not conflate feature computations, quoting policy and data history persistence inside one class.**
