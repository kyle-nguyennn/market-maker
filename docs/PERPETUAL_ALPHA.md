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

Install the **core + Hyperliquid** Conda environment from the repository root:

```bash
conda env create --format environment-yaml -f environment.hyperliquid.yml
conda activate market-maker
```

Use `environment.yml` instead for the **core-only** environment without
exchange SDKs. The Hyperliquid environment installs the shared packages via
`requirements/common.txt`, along with `hyperliquid-python-sdk==0.24.0`
from `requirements/hyperliquid.txt`, matching the competition guide.
Core model/strategy code remains independent of the SDK.

To add Hyperliquid to an existing `market-maker` Conda environment:

```bash
conda env update --format environment-yaml -n market-maker -f environment.hyperliquid.yml
```

## Current scope: SDK integrated, no live order validation yet

The Hyperliquid client now uses the official SDK 0.24.0 and supports:

- REST: par market specifications, par asset contexts (mark/oracle/funding),
  par L2 books/mids, USDPR spot collateral, par positions/open orders,
  par-filtered fills and individual order status.
- WebSocket: l2Book, bbo, trades, activeAssetCtx, userFills, orderUpdates,
  userEvents and userFundings. These subscriptions return raw SDK messages.
- Signed operations: one GTC/ALO/IOC limit order, cancel by OID/client order
  ID, and explicit leverage updates. These cannot run without
  connect_exchange() using the separately authorized API wallet.
- All order and cancellation results validate the nested status, including
  status=ok responses containing a per-operation error.
- Testnet and dex selection are hardcoded guards against accidental trading
  on another network or on another dex. Keys never appear in files or logs.

The SDK's Info.meta_and_asset_ctxs() convenience method in 0.24.0 defaults
to the native dex; the client explicitly calls Info.post("/info", ...) with
dex="par" instead. For authenticated trading, Exchange receives both
perp_dexs=["par"] and account_address=TEAM_WALLET.

### Install and verify REST connectivity (no order placement)

From the repository root:

~~~bash
conda env create --format environment-yaml -f environment.hyperliquid.yml
conda activate market-maker
python -m pytest -q
export PERPETUAL_ALPHA_TEAM_WALLET=0x<your-team-public-address>
python -m apps.perpetual_alpha --check --instrument BTC
~~~

The diagnostic queries the public par metadata, l2Book and asset contexts,
and reads the wallet's spot balance, par positions and par orders. It has no
order placement, cancellation or leverage commands.

### Example: enable signed operations programmatically

Set the *separately approved agent* key in your local secret store or process
environment. Do not use the master/team wallet private key. Do not check in
a .env file containing secrets.

~~~python
import os
from exchanges.hyperliquid.client import HyperliquidVenueClient
from exchanges.hyperliquid.types import OrderSide, TimeInForce, VenueOrderRequest

client = HyperliquidVenueClient()
client.connect_exchange(
    team_wallet=os.environ["PERPETUAL_ALPHA_TEAM_WALLET"],
    api_private_key=os.environ["PERPETUAL_ALPHA_API_PRIVATE_KEY"],
)
# Exchange is now configured, but nothing has been sent.
# Inspect query_meta(), query_l2_book(), account balances and risks first.
# For example, AFTER deliberate authorization to trade:
# request = VenueOrderRequest(
#     instrument="par:BTC", side=OrderSide.BUY,
#     price="60000", size="0.0002", tif=TimeInForce.ALO,
#     client_order_id="0x" + "0" * 31 + "1",
# )
# result = client.place_order(request)
# if result.status == "resting":
#     client.cancel_order("BTC", order_id=result.order_id)
~~~

Market-facing requests here contain strings. Decimal sizing, quantization
and side-specific price rounding remain the adapter's responsibility:
build an internal OrderIntent and call HyperliquidMarketAdapter.to_venue_order()
with HyperliquidMarketSpec from the actual market metadata.

### Open integration tasks

1. Confirm live REST smoke output on the funded account; check that USDPR is
   present and the par metadata lists the ten competition markets.
2. Confirm that the API wallet remains authorized using the Hyperliquid
   interface, then exercise one deliberately tiny post-only order and cancel
   during the practice window. **No real orders were placed during this change.**
3. Normalize SDK WebSocket messages into the Decimal market/account models,
   handle history deduplication, reconnect/resubscribe/resync, and persist
   replayable market data.
4. Implement OrderManager.reconcile(), stale-data kill switch and hard risk
   limits before the quoter can generate signed traffic.
5. Add kill-switch tests, network interruption tests, and paper-trading replay
   before deploying an unattended process.

The SDK's own WebSocket manager is not a production-grade replacement for
explicit reconnect, snapshot resync, event deduplication and stale-feed
monitoring. Do not run an unattended trading strategy yet.
