# Perpetual Alpha integration scaffold

This branch reuses the existing market-maker framework while keeping the
internal trading model independent of Hyperliquid.

## Architecture

```text
Hyperliquid REST / WebSocket / SDK
              |
              v
exchanges/hyperliquid/
              |
              v
market_handler/hyperliquid_adapter.py
      venue -> internal Decimal model
              |
              v
           model/
      Decimal MarketState
      Decimal Quote/OrderIntent
              |
      +-------+--------+
      |                |
      v                v
   quoter/       execution/
      |                |
      +-------+--------+
              |
              v
            risk/
              |
              v
 HyperliquidMarketAdapter
 internal -> venue conversion
              |
              v
exchanges/hyperliquid/
```

## Numeric model boundary

The internal model uses `Decimal` for all prices, quantities, rates, notionals,
and monetary values. Strategy, risk, portfolio, and execution logic never
depend on an exchange's wire types.

Each market adapter owns both directions of conversion:

- inbound venue values -> internal `Decimal` domain objects
- internal `Decimal` intents -> venue-specific price/size/symbol types

For Hyperliquid specifically, the adapter owns the `par:` symbol prefix,
`szDecimals` size quantization, price precision/significant-figure rules,
side mapping, and decimal-string serialization.

For RTG, the RTG adapter should similarly convert the same internal Decimal
objects to integer cents/ticks only at its venue boundary.

## Competition-specific invariants

- Network: Hyperliquid testnet
- DEX: `par`
- Collateral: `USDPR`
- Venue market names are prefixed, e.g. `par:BTC`
- Internal market names are unqualified, e.g. `BTC`
- Market-wide and position queries must explicitly select `dex=par`
- Team wallet is used for account queries
- Trading actions are signed by a separate approved API wallet
- Never commit or log private keys
- Inspect per-operation order results even when the request status is `ok`

## Hyperliquid-specific dependencies

Install only when building/running the Hyperliquid venue path:

```bash
pip install -r requirements/hyperliquid.txt
```

The file currently pins `hyperliquid-python-sdk==0.24.0`, matching the competition guide. Core model/strategy code does not depend on the SDK.

## Current scope

The scaffold is intentionally **read-only** before competition collateral is
funded. Signed actions remain unimplemented.

## P0 implementation order

1. Wire official Hyperliquid SDK 0.24.0 into `HyperliquidVenueClient`.
2. Query `meta`, `metaAndAssetCtxs`, `l2Book`, balance, positions and orders.
3. Add WebSocket subscriptions for book, trades, reference-price context and
   account events.
4. Normalize those messages into `model.MarketState`.
5. Add structured market-data recording and reconnect/resync.
6. After funding: approve an API wallet and wire signed order/cancel actions.
7. Convert internal `OrderIntent` through the adapter and validate one tiny
   ALO order end-to-end before plugging in a live quoter.
