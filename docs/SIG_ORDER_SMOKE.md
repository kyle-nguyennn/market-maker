# SIG Predictions — authenticated order/cancel smoke

**Status:** Attended diagnostic; one controlled buy, not an automated maker.
The existing `python -m apps.sig_predictions --check` only verifies API reads.
This procedure exercises signed single-order submission and targeted cancel.

## Prerequisites

- A SIG participant API key with trade permission; do not print or commit it.
- Correct tournament UUID and numeric tradable **exchange ID** (not market ID).
- Valid, recently updated, two-sided YES book and no prior open orders on that exchange.
- Check tournament eligibility with SIG before scored participation.
- Run from the repository root. The script uses the same SIG_PREDICTIONS_API_KEY as --check.

## Commands

```bash
# Retrieve the tournament UUID without any trading action:
python -m apps.sig_predictions --check --tournament-slug midterm-elections
export SIG_PREDICTIONS_TOURNAMENT_ID='<UUID reported above>'
python -m apps.sig_predictions --check --exchange-id <exchange-id>

# No POST or DELETE; validates scope, account access, book and open orders
python -m apps.sig_order_smoke --exchange-id <exchange-id>

# One real BUY YES 1 share @ 0.005, then cancel by returned order ID
python -m apps.sig_order_smoke --exchange-id <exchange-id> \
  --live --confirm-exchange-id <exchange-id> --accept-fill-risk

# If a cancellation fails and the state file has an order ID, recover only it
python -m apps.sig_order_smoke --exchange-id <exchange-id> \
  --live --confirm-exchange-id <exchange-id> --accept-fill-risk --recover
```

The `--tournament-slug midterm-elections` diagnostic calls `get_tournament` and prints its returned UUID when present.
Resolve exchange IDs using `list_markets` / `get_market`; the IDs shown in older unit
tests are **examples**, not authorized targets.

## Verifiable outcome

- POST uses a persisted unique `idempotencyKey`, scoped tournament/exchange,
  quantity 1 and YES limit price 0.005. It includes expirationDate about 120s
  in the future, which is not an independent cancellation guarantee.
- Script stores the returned orderId and any immediately traded quantity.
- Script DELETEs only that orderId, then GETs its terminal status.
- A terminal order may have filled first. Independently inspect
  `GET /orders/{id}/fills` and portfolio positions afterward.

## Safety and failure recovery

- Dry run is default. Live requires three explicit flags.
- SIG has **no post-only guarantee**: price may cross after the preflight.
- Reject stale, missing/crossed books, near-crossing quotes, and existing scoped orders.
- Persist local state to `data/state/sig-smoke-<tournament>-<exchange>.json`
  with fsync **before** POST. A second attempt against the same state file is rejected.
- **Never blindly retry** a POST after timeout or 409/429/502/503.
  An unknown submission may already have created an order.
- If POST outcome is ambiguous before OID capture, journal remains PREPARED;
  `--recover` cannot invent an OID. Use authoritative open-order/fill/position
  queries and manual review. Do not submit another order until resolved.
- If cancel acknowledgement or status is missing, journal retains the
  OID and `--recover` checks/targets only that order.
- The local file is a smoke-test safety record, not the full SIG order ledger.
  Do not delete it to evade an unresolved-state block.

**Not a live-strategy signoff.** A real SIG trading runtime still needs
tournament-scoped portfolio/cash/correlated-outcome risk, API budgets,
multi-leg order lifecycle, durable order state, realtime recovery, and ops alerting.
