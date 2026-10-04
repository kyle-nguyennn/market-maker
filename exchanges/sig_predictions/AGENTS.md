# SIG Predictions adapter rules

- Use only the documented `/api/v1` interface; never scrape WebUI `/api/*`.
- Keep API keys in environment variables and never log them.
- Authentication is `Authorization: Bearer <key>` (the API also accepts
  `X-API-Key`).
- A Market is a container; an Exchange is the atomic tradable contract. Core
  instruments therefore identify `exchangeId`, not `marketId + outcome`.
- REST order books are YES-normalized. Keep the internal book YES-normalized.
- For ordinary two-sided quoting map BID -> BUY YES at p and
  ASK -> BUY NO at 1-p.
- Do not silently claim post-only semantics: the API has no post-only field.
- Quantities are positive integers <= 2,147,483,647.
- Fresh limit prices are 0.005..0.995 on the 0.005 tick.
- Every placement needs a client-supplied idempotency key. Preserve it across
  retries of the identical logical request.
- Prefer atomic `POST /orders/multi-leg` for a two-sided quote pair.
- Re-quote with scoped `POST /orders/cancel-all`; do not treat local state as
  flat until it returns HTTP 200.
- Treat Realtime as best-effort. REST is authoritative; resync after gaps,
  reconnects, token refresh, socket errors, `resyncRequired`, and periodically.
- Preserve per-exchange `asOf {sequence, at}` versions.
- Internal strategy/risk/portfolio numerics remain Decimal; float conversion
  happens only at the SIG HTTP boundary.
