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

For Hyperliquid specifically, the adapter owns the selected `par:`/`pa:` symbol prefix,
`szDecimals` size quantization, price precision/significant-figure rules,
side mapping, and decimal-string serialization.

For RTG, the RTG adapter should similarly convert the same internal Decimal
objects to integer cents/ticks only at its venue boundary.

## Competition-specific invariants

- Both DEXs currently use Hyperliquid **testnet**.
- Practice DEX: `par`, collateral `USDPR`, e.g. `par:BTC`.
- Scored DEX: `pa`, collateral `USDP`, e.g. `pa:BTC`.
- Select the whole profile via `.env` (`PERPETUAL_ALPHA_DEX=par|pa`) or the CLI `--dex` flag.
- Internal market names are unqualified, e.g. `BTC`
- Market-wide and position queries must explicitly select the configured dex (`par` or `pa`)
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

## SDK connectivity and one-order practice smoke (no automated live quoting)

The Hyperliquid client now uses the official SDK 0.24.0 and supports:

- REST: selected DEX market specifications and asset contexts (mark/oracle/funding),
  L2 books/mids, USDPR (par) or USDP (pa) spot collateral, DEX-scoped positions,
  open orders, filtered fills and individual order status.
- WebSocket: l2Book, bbo, trades, activeAssetCtx, userFills, orderUpdates,
  userEvents and userFundings. These subscriptions return raw SDK messages.
- Signed operations: one GTC/ALO/IOC limit order, cancel by OID/client order
  ID, and explicit leverage updates. These cannot run without
  connect_exchange() using the separately authorized API wallet.
- All order and cancellation results validate the nested status, including
  status=ok responses containing a per-operation error.
- The config validates selected network, host, WebSocket URL, DEX and collateral
  as one profile; mismatched combinations fail before SDK initialization.
  Selecting pa never authorizes signed trading: `PERPETUAL_ALPHA_PA_ALLOW_TRADING`
  defaults to `false`. Keys never appear in files or logs.

The SDK's Info.meta_and_asset_ctxs() convenience method in 0.24.0 defaults
to the native dex; the client explicitly calls Info.post("/info", ...) with
dex=client.config.dex instead. For authenticated trading, Exchange receives
perp_dexs=[client.config.dex] and account_address=TEAM_WALLET.

### Install and verify REST connectivity (no order placement)

From the repository root:

~~~bash
conda env create --format environment-yaml -f environment.hyperliquid.yml
conda activate market-maker
python -m pytest -q
cp .env.template .env
# Select PERPETUAL_ALPHA_DEX=par or pa in .env
python -m apps.perpetual_alpha --check --instrument BTC
# Temporarily override the selection:
python -m apps.perpetual_alpha --check --dex pa --instrument BTC
~~~

The diagnostic queries the **selected** DEX metadata, L2 and asset contexts,
and reads the team's correct spot collateral token, scoped positions and
orders. The local .env is loaded automatically via python-dotenv. It has no
order placement, cancellation or leverage commands.

### Example: enable signed operations programmatically

Set the *separately approved agent* key in your local secret store or process
environment. Do not use the master/team wallet private key. Do not check in
a .env file containing secrets.

~~~python
import os
from exchanges.hyperliquid.client import HyperliquidVenueClient
from exchanges.hyperliquid.types import OrderSide, TimeInForce, VenueOrderRequest

client = HyperliquidVenueClient()  # selected .env profile
# For pa, explicitly verify funding, account approval, risk limits, and set
# PERPETUAL_ALPHA_PA_ALLOW_TRADING=true before connecting a signer.
client.connect_exchange(
    team_wallet=os.environ["PERPETUAL_ALPHA_TEAM_WALLET"],
    api_private_key=os.environ["PERPETUAL_ALPHA_API_PRIVATE_KEY"],
)
# Exchange is now configured, but nothing has been sent.
# Inspect query_meta(), query_l2_book(), account balances and risks first.
# For example, AFTER deliberate authorization to trade:
# request = VenueOrderRequest(
#     instrument=client.config.venue_symbol("BTC"), side=OrderSide.BUY,
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

1. Confirm read-only pa metadata and the team's scored USDP funding. On
   2026-10-15 the testnet pa metadata returned only BTC/ETH/HYPE (3 of 10
   announced markets); re-check deployment for all ten before launch.
2. PR #5 validated one deliberately tiny `par:BTC` resting post-only order and
   cancel. This does **not** validate fills, continuous operation or scored `pa`
   authorization. Independently verify revocation of the previously exposed key.
3. Normalize streaming SDK messages into the Decimal market/account models,
   handle history deduplication, reconnect/resubscribe/resync, and persist
   replayable market data. REST L2/asset-context normalization is already done.
4. Integrate the new pure OrderManager/risk/gateway into an event loop with
   durable journaling, SDK order/fill event normalization, capacity parsing and
   real account-mode validation. The default gateway remains dry-run.
5. Add kill-switch tests, network interruption tests, and paper-trading replay
   before deploying an unattended process.

The SDK's own WebSocket manager is not a production-grade replacement for
explicit reconnect, snapshot resync, event deduplication and stale-feed
monitoring. Do not run an unattended trading strategy yet.

### Empirical scored DEX discovery (2026-10-15)

A read-only SDK query to `https://api.hyperliquid-testnet.xyz/info`
with `dex=pa` returned `pa:BTC`, `pa:ETH`, and `pa:HYPE`.
The same query against `https://api.hyperliquid.xyz/info` returned
HTTP 500. **Both configured DEXs therefore use testnet for now.**
The announced seven additional `pa` markets were not visible yet.
The October 15 SDK L2 probe showed no valid two-sided `pa:BTC` book (mid=None),
so scored connectivity is **not** scored trading readiness.
Do not treat DEX discovery as proof of team USDP funding or trading readiness.
See `notes/20261015_perpetual_alpha_competition_rules.md` for the
organizer's complete rules and the remaining engineering gaps.


## Order/risk increment (draft PR #6, October 15, 2026)

The branch `feat/perpetual-alpha-order-risk-quoter` now contains:

- `model/order.py`: venue-independent `OrderEvent`, `PlaceAction`,
  `CancelAction`; cumulative-size conventions and a nonterminal cancel ACK.
- `execution/order_manager.py`: deterministic, fail-closed order slots,
  cancel/wait/replace, 128-bit CLOIDs, late/duplicate cumulative update
  handling, explicit startup resynchronization and unknown-state quarantine.
  `reconcile()` **reserves** proposed actions but never calls the SDK.
- `risk/risk_manager.py`: post-only price collar, market age, per-order
  min/max notionals, simultaneous all-fill inventory bounds, gross/net
  notionals, verified account+margin+capacity, absolute drawdown kill latch.
  `max_drawdown` is in collateral currency units, NOT percent.
- `execution/safe_gateway.py`: a dry-run-by-default dispatch boundary.
  Live mock testing requires separate `live_enabled` and
  `operator_authorized`; actual scored-DEX signing is still guarded by
  `PERPETUAL_ALPHA_PA_ALLOW_TRADING=false`. It rechecks venue-quantized
  price/size and derives all fillable orders from its OrderManager, including
  pending cancels, immediately before any potential SDK call.
- `portfolio/accounting.py`: idempotent trade fill-ID application, including
  maker rebates. Its single-instrument cash/position proxy is **not** a
  substitute for verified perpetual-margin account equity.

**Not yet integrated / unsafe for unattended trading:** SDK `orderUpdates`
and `userFills` shape normalization; a race-aware REST/WS recovery anchor;
persistent action journal and CLOID counter; `activeAssetData` capacity and
Standard/Unified account-mode mapping; cancel retry/backoff/API budgets;
emergency-cancel orchestration on stale data; P1 strategy and replay runner.
An accepted cancel response is never interpreted as proof of a closed order.
All `null` hard limits in `config/perpetual_alpha.yaml` still prevent
successful new-order risk checks, as intended.

Local offline validation:

```bash
python -m pytest -q tests/test_order_manager.py tests/test_risk_manager.py \
  tests/test_safe_execution.py tests/test_portfolio_fills.py
PERPETUAL_ALPHA_DEX=pa python -m pytest -q
```

A passing mocked send-path test does not authorize live execution. Refer to
`notes/20261015_perpetual_alpha_order_risk_quoter_handoff.md` for the remaining
scored-pa go/no-go gates.
