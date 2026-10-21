# SIG Predictions adapter rules

- Use only the published `super-market==0.1.0` Scalar SDK resources.
  Do not recreate a manual urllib/httpx REST transport or scrape WebUI `/api/*`.
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
- Every single placement, batch or multi-leg placement requires its own
  **durable top-level** idempotency key. Do not require per-leg keys in atomic
  batch/multi-leg payloads. Preserve the logical key across manual recovery.
- Prefer atomic `POST /orders/multi-leg` for a two-sided quote pair.
- Re-quote with scoped SDK `orders.create_cancel_all`; do not treat local
  state as flat until the venue acknowledges and reconciliation confirms.
- Treat Realtime as best-effort. REST is authoritative; resync after gaps,
  reconnects, token refresh, socket errors, `resyncRequired`, and periodically.
- Preserve per-exchange `asOf {sequence, at}` versions.
- Internal strategy/risk/portfolio numerics remain Decimal; float conversion
  happens only at the SIG HTTP boundary.

- Construct SIG SDK with `max_retries=0` for **all** clients: its default
  automatic retries can duplicate/obscure uncertain signed requests.
- Keep `SigPredictionsVenueClient` read-only unless `allow_trading=True`
  is explicitly authorized by a durable, risk-gated execution path.
- Normalize SDK models to strategy-facing contracts in the facade only;
  do not embed transport serialization in the strategy or order manager.
