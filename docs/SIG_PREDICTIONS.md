# SIG Predictions Cup integration

## Source of truth

The adapter is based on the official OpenAPI 3.1 document exported from:

- https://sig.thesuper.market/api/v1/docs#description/introduction

Production server declared by that document:

    https://www.thesuper.market/api/v1

Authentication:

    Authorization: Bearer <key>

The API also accepts `X-API-Key: <key>`. Participant bots need `read` for
market/account data and `trade` for order placement/cancellation. Website
`/api/*` routes outside `/api/v1` are not a supported automation interface.

## Model mapping

The important API distinction is:

- **Market** = container.
- **Exchange** = atomic tradable contract.
- Every order, trade, position, and order book references an `exchangeId`.
- Exchange order books and trade prices are YES-normalized.

The internal instrument is therefore:

    SIG_EXCHANGE:<exchange_id>

The shared strategy model remains YES-normalized:

- internal BID at `p` -> API `buy yes` at `p`
- internal ASK at `p` -> API `buy no` at `1 - p`

Using BUY NO for the ask is the clean quoting representation recommended by
the API's market-making workflow. The engine can canonicalize certain SELL
orders into complement BUY orders, but the adapter does not depend on that.

## Order contract

Single order:

    POST /orders

Required body fields:

- `idempotencyKey`
- `exchangeId` (numeric string)
- `side`: `yes` or `no`
- `action`: `buy` or `sell`
- `quantity`: positive integer, max 2,147,483,647

Optional:

- `price`
- `expirationDate`
- `tournamentId`

Fresh limit prices are on a 0.005 tick and must lie from 0.005 through 0.995.
Omitting `price` is a market order. `price=1` for BUY and `price=0` for
SELL also encode market orders.

The API has no post-only request field. The SIG adapter therefore refuses an
internal `post_only=True` intent instead of silently weakening the guarantee.
For live quoting, explicitly opt out of post-only only after the execution
layer has a deliberate crossing policy.

For two-sided quote publication prefer:

    POST /orders/multi-leg

It supports 1-10 legs atomically. For bulk non-atomic entry,
`POST /orders/batch` supports 1-50 orders.

## Market data

Primary reads:

- `GET /markets`
- `GET /markets/{id}`
- `GET /exchanges/{id}/price`
- `GET /exchanges/{id}/orderbook?depth=200&tournamentId=...`

The exchange order book returns YES-normalized `bids` and `asks` and an
`asOf = {sequence, at}` engine version where available.

For a competition tournament, resolve its slug with
`GET /tournaments/{slug}` and carry the returned UUID as `tournamentId`
through market discovery, exchange reads, and order placement.

## Realtime

Mint credentials with:

    POST /realtime/token

The response contains `token`, `expiresAt`, `supabaseUrl`, `anonKey`,
and `channels.user`. Tokens last 3 hours.

For a few tournament markets subscribe to:

    tournament:{tournament_id}:market:{market_id}

For many markets use the single aggregate channel:

    tournament:{tournament_id}:markets

Market/account changes are coalesced into one-second batches. Realtime is
best-effort, not authoritative. Maintain topic revisions, apply versioned
books by `asOf`, and resynchronize from REST on initial subscription,
reconnect, token refresh, socket error, revision gap, `resyncRequired`, a
trade with null sequence, and periodically (the spec suggests every 1-2 min).

## Re-quote and cancellation

For a market-making loop, cancel with:

    POST /orders/cancel-all

Scope by `exchangeId` whenever possible. Only post replacement quotes after
cancel-all returns HTTP 200, which means no targeted order remains resting.

A `503 SERVICE_UNAVAILABLE` can represent a partially completed cancel-all;
`error.details.cancelled` reports confirmed progress. Retry the same request
with exponential backoff until HTTP 200.

## Idempotency and retry policy

`POST /orders`, `POST /orders/batch`, and `POST /orders/multi-leg` require
client-supplied idempotency keys. Reuse the same key only for an identical
logical request.

Important retry cases:

- 429 `RATE_LIMITED`: honor `Retry-After`; retry same idempotency key.
- 409 `REQUEST_IN_FLIGHT`: wait for the execution lease, then retry identical
  request with the same key.
- 502 `ORDER_STATUS_UNKNOWN`: the order may already exist; reconcile
  positions/order state or retry the identical request with the same key.
- 503 `TX_CONFLICT` / `SERVICE_UNAVAILABLE`: exponential backoff with
  jitter, preserving the idempotency key.
- Other 4xx responses are not automatically retryable.

Standard participant accounts share a per-account budget of 100 reads and
30 writes per minute. Open Realtime connections do not consume that budget.

## Portfolio reconciliation

Default-scope reads include:

- `GET /portfolio/positions`
- `GET /portfolio/pnl`
- `GET /portfolio/fills`

For an explicit non-default competition tournament, use the tournament-scoped
portfolio paths, e.g.:

    GET /tournaments/{slug}/portfolio/positions

This matters because default portfolio endpoints do not accept an arbitrary
`tournamentId` for positions/P&L.

## Implementation status

Implemented:

1. authenticated v1 REST transport;
2. market discovery and exchange price/book reads;
3. exact single-order payload and response normalization;
4. batch and atomic multi-leg placement;
5. order listing/detail/fills;
6. single cancel and scoped cancel-all;
7. default and tournament portfolio positions/P&L;
8. realtime token minting and channel naming;
9. Decimal-to-wire conversion, tick/quantity validation, and execution result
   normalization.

Next runtime layer:

1. Supabase Realtime connection/reconnect manager;
2. `revision` / `previousRevision` gap detector;
3. per-exchange `asOf` book version reconciliation;
4. periodic REST resync scheduler;
5. quote-cycle orchestration: cancel-all -> confirmed 200 -> atomic multi-leg
   repost;
6. strategy/risk integration using tournament balance and positions.

## Mainline migration (2026-10-21)

The former PR #2 scaffold is ported onto mainline without replacing the
merged, fail-closed OrderManager from PR #6.

- Shared OrderManager accepts canonical contract IDs like SIG_EXCHANGE:36.
- 128-bit generated order identity can also be used as SIG idempotency key.
- ExecutionStatus now covers partial fills, cancels and accepted NOOP.
- SIG client defaults to read-only; signed requests require explicit
  allow_trading=True. This is a belt, **not a pretrade risk gateway**.
- CLI python -m apps.sig_predictions --check uses authenticated GET reads.
- SIG Midterm Elections trading ends November 4, noon ET.

### Important differences from Hyperliquid

| Aspect | Hyperliquid | SIG Predictions |
| --- | --- | --- |
| Instrument | logical BTC, ETH | SIG_EXCHANGE:<exchange_id> |
| Quote | linear perp bid/ask | YES probability; ASK -> BUY NO at (1-p) |
| Post-only | supported ALO | unsupported |
| Size | Decimal precision | positive integer shares |
| Risk | leverage/funding/margin | SUSQie cash, binary event correlations |
| Limit price | asset-specific | 0.005–0.995, tick 0.005 |
| Idempotency | venue CLOID | required opaque key, finite retention |
| Streaming | Hyperliquid WS | best-effort batched realtime and REST resync |

SIG October 17 changelog: multi-leg 502 ORDER_STATUS_UNKNOWN requires
authoritative reconciliation or identical-key retry; completed request
responses are stored for 48h, after which 409 IDEMPOTENCY_KEY_EXPIRED
prevents replay. Do not blindly create a new key for an unknown order.
Rate budget is 100 reads / 30 writes per minute per ordinary account.
Source: https://sig.thesuper.market/docs/changelog

### Remaining before unattended trading

1. Check participant API key using read-only CLI; resolve tournament scope.
2. Implement realtime token/socket/revisions and REST resynchronization.
3. Enforce SIG-specific cash/position/scenario risk using server state.
4. Persist idempotency keys before POST and wire order/fill/cancel events.
5. Test lost ACK, partial fills, 409/429/502/503, restart recovery.
6. Then explicitly enable signed actions behind the actual send gateway.

Until those are done, do not deploy an unattended scored bot.

## Competition eligibility preflight — blocking human check

Before using the scored tournament, verify eligibility directly against the
[Official Rules](https://predictionscup.com/rules/) (last updated
September 30, 2026). Requirements include residence in an eligible
jurisdiction, active qualifying student enrollment, and individual (not team)
participation. **Singapore does not appear in the eligible residence list.**
The rules also specify study at an accredited institution in the participant's
country of residence; cross-border/online enrollment deserves explicit
clarification rather than an assumption.

Do not infer contest/prize eligibility from successful registration, API
authentication, or a trading balance. For an exception or eligibility
interpretation, contact predictionscup@sig.com before placing scored orders.

Source: https://predictionscup.com/rules/
