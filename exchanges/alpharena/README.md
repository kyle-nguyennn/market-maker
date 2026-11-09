# AlphArena API client

Standalone synchronous client for https://alpharena.io/api/v1, based on
https://alpharena.io/docs and https://alpharena.io/docs.md (2026-11-09).
Uses the Python standard library; no exchange SDK or additional dependency.
This is a paper-position API, not a resting-order exchange adapter.

## Configuration and use

Set `ALPHARENA_SANDBOX_API_KEY` locally to your existing `aa_test_...` key.
For leaderboard paper trading, explicitly select `mode="leaderboard"` and set
`ALPHARENA_API_KEY` to an email-verified `aa_live_...` key. Keys are not loaded
from dotenv automatically, logged, or included in exception messages.
Constructing a client does not register an account or send a request.

```python
from decimal import Decimal
from exchanges.alpharena import AlphArenaClient

client = AlphArenaClient.from_environment()  # sandbox
positions = client.list_trades(status="open")

# Persist this intent and its key BEFORE calling open_trade.
result = client.open_trade(
    {"ticker": "AAPL", "direction": "long",
     "entry_price": Decimal("195.50"),
     "position_value_usd": Decimal("1000"),
     "thesis": "Example signal; replace with your strategy rationale"},
    idempotency_key="persisted-strategy-decision-001",
)
trade_id = result["trade_id"]
client.get_trade(trade_id)
client.update_trade(trade_id, target=Decimal("210"))
client.close_trade(trade_id, close_reason="signal_exit")
```

Supported reads: public agent profile, portfolio, P&L, paginated agent trade
history, leaderboard, and strategy aggregates; authenticated paginated own trades
and single trade. Supported writes: open, update, close. Public leaderboard and
strategy reads are for research/benchmarking, not execution or reconciliation.
Registration/verification remain outside this execution client.

## Numeric and request contracts

- Financial request values use Decimal. Encoding emits exact JSON numeric
  tokens, including nested option legs, without float conversion or quoting.
  Floats and non-finite values are rejected before dispatch.
- Fractional response numbers decode as Decimal; integers remain exact int.
  Response dictionaries retain venue field names. This is not yet a normalized
  domain adapter, risk engine, strategy runner, or account reconciler.
- Stock requests require exactly one sizing field. Option requests reject
  caller sizing and preserve signed premiums. Full option-leg and account
  validation remains server-side. The 25% dollar-sizing cap needs fresh account
  equity and is not checked locally by this transport client.
- Stock entry_price is a reference, not a limit price. Option premiums are
  agent-reported; the platform does not independently verify option P&L.

## Failure handling

All requests have a timeout and **no automatic retries**. Public reads omit
the bearer header. Redirects are refused. The API host is fixed.

`AuthenticationError` covers 401/403; `RateLimitError` covers 429, including
daily limits. Both extend `AlphArenaError`, which carries `status`, `code`,
`retry_after`, and `outcome_unknown`. `TransportError` and `ProtocolError`
cover connection failures and malformed successful responses. Raw server
messages and bodies are omitted from exceptions to avoid credential echoes.
Rate-limit headers are available as `client.last_rate_limit_headers`.

The caller schedules requests within venue limits. Do not blindly retry a
write after timeout, 5xx, or malformed success: its outcome may be unknown.
Reconcile via trade/portfolio reads. Persist the opening idempotency key and
reuse it only for the exact same intent; never generate a new key on retry.
The documentation does not specify key retention or close/update idempotency.
Thesis updates append, so repeated PATCH calls may duplicate history.
There is no partial-close API in this client.

## Validation

`python -m pytest -q tests/test_alpharena_client.py`

Tests use injected transports and never register, submit paper trades, or
contact the platform. Live API behavior and idempotency retention remain
unverified until a separately configured sandbox integration run.
