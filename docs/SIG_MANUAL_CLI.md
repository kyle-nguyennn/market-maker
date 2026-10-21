# SIG Predictions — manual API v1 quickstart console

The published Scalar SDK (`super-market==0.1.0`) provides REST operations and
the `POST /realtime/token` credential minting call. Our manual CLI uses
the SDK for **all REST calls** and the documented Supabase/Phoenix Realtime
WebSocket protocol to stream channel events. It prints one JSON object per
line to stdout (suitable for `jq`), never logs API keys or JWTs, and never
submits a signed order without explicit live confirmations.

Scalar dashboard Quickstart is not currently anonymously retrievable from
the development environment; this runbook is grounded in the versioned
generated SDK's typed method signatures plus the official SIG API guide
and Supabase Realtime protocol, not a verbatim transcription of that panel.

## Install and authenticate

From the repository root:

```bash
python -m pip install -r requirements/sig_predictions.txt
export SIG_PREDICTIONS_API_KEY='<key stored locally, never commit>'
python -m apps.sig_cli --help
python -m apps.sig_cli account
```

Use the participant API key's actual permission scope. Neither a valid API
key nor contest registration establishes prize/competition eligibility;
verify that independently with SIG before trading in a scored tournament.

## Step 1 — Find active tournament UUID and slug

```bash
python -m apps.sig_cli tournaments --status active
python -m apps.sig_cli tournaments --status any
```

Each record shows the exact `id` (UUID), `slug`, `name`, `status`,
start/end dates and balance. More than one tournament may be accessible;
never automatically assume the first row is the competition you want.
Copy the real UUID and slug displayed by SIG:

```bash
export SIG_PREDICTIONS_TOURNAMENT_ID='<actual UUID from tournaments>'
export SIG_TOURNAMENT_SLUG='<actual slug from tournaments>'
```

## Step 2 — Market and exchange discovery

Market is the enclosing question, while each Exchange is an atomic tradable
contract. A market can have multiple exchanges; don't substitute the market
ID in an order. Realtime channels are keyed by **market ID**.

```bash
python -m apps.sig_cli markets --tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID" --status open --pages 3
python -m apps.sig_cli markets --tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID" --search election
python -m apps.sig_cli exchanges --tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID"
python -m apps.sig_cli market '<market ID>' --tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID"
python -m apps.sig_cli book '<exchange ID>' --tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID"
```

Output includes market ID, exchange ID, option, price, context, and YES-
normalized bids/asks with version information when available. List commands
support `--limit` and `--pages` and page via the documented API cursors.

## Step 3 — Inspect account, positions and open orders

```bash
python -m apps.sig_cli positions --tournament-slug "$SIG_TOURNAMENT_SLUG"
python -m apps.sig_cli orders --tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID" --status open
```

Account and portfolio APIs can return different balances in different
tournament contexts. Always use an explicit tournament slug for positions
and an explicit tournament UUID for order reads/writes.

## Step 4 — Preview and manually submit exactly one limit order

SIG has **no post-only flag**, so even a limit order may execute.
Market orders are intentionally **not** exposed by this manual CLI.
Limits must be 0.005–0.995 in 0.005 ticks and quantity is an integer.

First run without `--live`:

```bash
python -m apps.sig_cli place \
  --tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID" \
  --exchange-id '<exchange ID>' --side yes --action buy \
  --quantity 1 --price 0.005
```

After checking the preview, the specific exchange/orderbook, eligibility,
and fill risk, use the explicit live flags:

```bash
python -m apps.sig_cli place \
  --tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID" \
  --exchange-id '<exchange ID>' --side yes --action buy \
  --quantity 1 --price 0.005 \
  --live \
  --confirm-exchange-id '<same exchange ID>' \
  --confirm-tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID" \
  --accept-fill-risk
```

The CLI fsyncs `data/state/sig-manual-*.json` (mode 0600) **before** the
SDK POST, with a unique idempotency key. It makes exactly one POST and
never blindly retries an ambiguous response. If the response is ambiguous,
preserve the state file and reconcile account orders/fills/positions before
any new placement. `expirationDate` is set 120 seconds ahead by default.
This does not guarantee that the order was cancelled after a crash.

## Step 5 — Inspect fills, track execution and cancel

Use the order ID returned by the server (not the exchange or market ID):

```bash
python -m apps.sig_cli order '<order ID>'
python -m apps.sig_cli watch-order '<order ID>' --interval 3 --polls 40

# First preview target and tournament scope:
python -m apps.sig_cli cancel '<order ID>' \
  --tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID"

# Explicitly cancel ONLY the specified order:
python -m apps.sig_cli cancel '<order ID>' \
  --tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID" \
  --live --confirm-order-id '<same order ID>'

python -m apps.sig_cli order '<order ID>'
python -m apps.sig_cli positions --tournament-slug "$SIG_TOURNAMENT_SLUG"
```

Watch polls GET order and order-specific fills, printing changes and aggregate
filled quantity; 2 seconds is the minimum polling interval to respect the
per-account rate budget. Pagination flags are exposed. A cancellation ACK
does **not** guarantee zero earlier executions.

## Step 6 — Realtime WebSocket market feed to stdout

The Scalar SDK only supplies Realtime **credentials**; the actual WS stream
speaks Supabase Phoenix protocol v2. The CLI mints a short-lived token
using SDK `realtime.create_token`, connects to the supplied project,
subscribes to a private channel and waits for `phx_reply` success.
Callbacks print safe/redacted JSONL. A 20-second Phoenix heartbeat keeps
the connection alive; connection interruptions are reported with a
warning that full REST resynchronization is needed.

```bash
# One market's updates — uses MARKET ID, not EXCHANGE ID:
python -m apps.sig_cli stream \
  --tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID" \
  --market-id '<market ID>' --seconds 300

# All markets in the tournament (markets_batch broadcast events):
python -m apps.sig_cli stream \
  --tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID" \
  --all-markets --seconds 300

# Private authenticated user channel:
python -m apps.sig_cli stream --user --seconds 300

# Capture JSONL stream without exposing credentials:
python -m apps.sig_cli stream \
  --tournament-id "$SIG_PREDICTIONS_TOURNAMENT_ID" \
  --all-markets --seconds 60 | tee sig-feed.jsonl
```

Optional `--max-events N` exits after N received events;
`--reconnects N` caps failed reconnects. Press Ctrl-C to stop.
Realtime is **best-effort**; missed messages, revisions and asynchronous
book updates must be resynchronized from REST before using them for
trading. This CLI is only a diagnostic listener, not a synchronized
market-data feed suitable for unattended quoting.

## Source references

- SIG's [API Overview](https://sig.thesuper.market/docs) and
  [API Changelog](https://sig.thesuper.market/docs/changelog).
- Generated [Super Market Python SDK](https://github.com/kyle-nguyennn/super-market-api).
- Supabase [Realtime Phoenix protocol](https://supabase.com/docs/guides/realtime/protocol).
