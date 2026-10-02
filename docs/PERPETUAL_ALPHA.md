# Perpetual Alpha integration scaffold

This branch adds the venue boundaries needed to reuse the existing
market-maker framework for ICAIF 2026 Perpetual Alpha.

## Architecture

```text
Hyperliquid REST / WebSocket / SDK
              |
              v
exchanges/hyperliquid/
              |
              v
market_handler/hyperliquid_adapter.py
              |
      normalized domain state
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
exchanges/hyperliquid/
```

## Competition-specific invariants

- Network: Hyperliquid testnet
- DEX: `par`
- Collateral: `USDPR`
- Market names are prefixed, e.g. `par:BTC`
- Market-wide and position queries must explicitly select `dex=par`
- Team wallet is used for account queries
- Trading actions are signed by a separate approved API wallet
- Never commit or log private keys
- Inspect per-operation order results even when the request status is `ok`

## Current scope

The scaffold is intentionally **read-only** before competition collateral is
funded. No signed action is implemented yet.

## P0 implementation order

1. Wire official Hyperliquid SDK 0.24.0 into `HyperliquidVenueClient`.
2. Query `meta`, `metaAndAssetCtxs`, `l2Book`, balance, positions and orders.
3. Add WebSocket subscriptions for book, trades, reference-price context and
   account events.
4. Normalize those messages through `HyperliquidMarketAdapter`.
5. Add structured market-data recording and reconnect/resync.
6. After funding: approve an API wallet and wire signed order/cancel actions.
7. Validate one tiny ALO order end-to-end before plugging in a live quoter.

## Known compatibility gap

`quoter/base_quoter.py` currently models price/quantity as integers because
the original RTG path used integer ticks. Hyperliquid has market-specific size
precision and price significant-figure rules. Do not push Hyperliquid rounding
into strategy code. The next compatibility change should introduce a
venue-neutral Decimal intent model with venue-specific quantization at the
adapter boundary.
