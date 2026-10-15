# Hyperliquid adapter notes

- Use Hyperliquid **testnet** only for Perpetual Alpha.
- Perpetual Alpha practice uses DEX `par` (USDPR); the scored competition DEX is `pa` (USDP). Select exactly one through `PERPETUAL_ALPHA_DEX=par|pa`.
- Venue names are `par:BTC` or `pa:BTC`; internal symbols remain `BTC` and `ETH`.
- Internal symbols are unqualified (`BTC`, `ETH`, ...).
- Internal numeric values are always `Decimal`.
- This adapter owns all Hyperliquid conversion: symbol prefix, side mapping,
  size quantization, price precision, and decimal-string serialization.
- Market-wide and position queries must explicitly select the configured DEX, including per-DEX market metadata and account state.
- `PERPETUAL_ALPHA_PA_ALLOW_TRADING=false` is the default signed-order guard; no execution path may bypass it.
- Treat an accepted cancellation as pending until reconciliation confirms terminal status; never infer no fills from a cancel ACK.
- Always validate capacity, mark/reference freshness, aggregate fillable exposure and account identity immediately before sending a new order.
- Use the team-wallet address for account queries.
- Signed bot actions use a separately approved API wallet.
- Never commit or log private keys, seed phrases, or API-wallet secrets.
- Keep SDK/raw API types inside `exchanges/hyperliquid/`.
- Strategy, risk, execution, and portfolio code must not import Hyperliquid SDK types.
