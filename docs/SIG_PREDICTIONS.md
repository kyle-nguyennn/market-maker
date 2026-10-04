# SIG Predictions Cup adapter scaffold

## Scope

This scaffold reuses the shared market-maker core and keeps SIG-specific
semantics at the exchange boundary. Internal prices, quantities, positions,
and execution values stay Decimal.

The current competition is the 2026 Midterm Elections competition. The public
Guide states that each participant starts with 100,000 SUSQies and the
competition runs from October 1, 2026 at noon Eastern through November 4,
2026 at noon Eastern.

## Public sources used

- Product Guide: https://sig.thesuper.market/docs
- API Reference: https://sig.thesuper.market/api/v1/docs#description/introduction
- Markets & Trading: https://sig.thesuper.market/docs/markets-and-trading
- Changelog: https://sig.thesuper.market/docs/changelog
- Official Rules: https://predictionscup.com/rules/

The API Reference is authoritative for exact endpoint paths, authentication
headers, request fields, response fields, and rate limits.

Do not reverse engineer the website endpoints. The October 1 changelog says
automated clients must use the v1 API with an API key.

## Market model

A market has YES and NO outcomes. Prices are in [0, 1], and a winning share
pays 1 SUSQie. The Guide also documents the complement relationship:
selling YES at p is economically equivalent to buying NO at 1-p.

The adapter intentionally preserves explicit outcome plus BUY/SELL intent
instead of rewriting complements. This keeps inventory, attribution, and
strategy decisions auditable.

Internal instrument format:

    SIG_PREDICTION:<market_id>:<YES|NO>

Example:

    SIG_PREDICTION:123:YES

## Order book and realtime state

REST order books have an asOf book version. Realtime tournament-market
messages use:

    tournament:{tournament_id}:market:{market_id}

The changelog says those updates arrive in one-second batches. Realtime trade
IDs match the REST trade tape.

When the live client is wired, reconciliation should:

1. snapshot REST book plus asOf;
2. subscribe to the market-specific realtime channel;
3. apply only updates consistent with the observed version sequence;
4. resnapshot on reconnect, version discontinuity, or ambiguous local state.

## Execution safety

The Guide allows both market and limit orders. Limit orders can rest until
filled or cancelled. Users can keep orders on both outcomes, but own orders do
not self-trade.

Cancellation has an explicit retry rule: HTTP 503 means the trading engine is
temporarily unavailable and retry is safe. Cancel-all should be retried until
the server returns 200. Local state must not be marked flat only because a
cancel request was sent.

The Official Rules define a competition trade as received only after the
platform validly receives and explicitly confirms it. Treat server
confirmation as authoritative for API-submitted orders.

## Wire-client status

The semantic adapter is implemented. Exact REST paths, auth-header names,
payload keys, response keys, and rate limits must be copied from the canonical
API Reference above; they are intentionally not guessed from WebUI traffic or
from the API documentation URL structure.

Before enabling live order submission, copy those values verbatim into
exchanges/sig_predictions/client.py and add contract tests using the examples
and schemas in the API Reference.

## Recommended implementation order

1. Wire read-only market discovery and order-book endpoints from API Reference.
2. Add API-key auth exactly as documented.
3. Add realtime subscription and REST/realtime reconciliation.
4. Wire positions and open orders.
5. Wire limit-order placement with one tiny canary order.
6. Wire single-order cancel and cancel-all with documented 503 retry behavior.
7. Only then connect the shared quoter/order manager to live execution.
