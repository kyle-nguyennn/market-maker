# Hyperliquid adapter notes

- Use Hyperliquid **testnet** only for Perpetual Alpha.
- The competition DEX is `par`; venue market names are `par:BTC`, `par:ETH`, etc.
- Internal symbols are unqualified (`BTC`, `ETH`, ...).
- Internal numeric values are always `Decimal`.
- This adapter owns all Hyperliquid conversion: symbol prefix, side mapping,
  size quantization, price precision, and decimal-string serialization.
- Market-wide and position queries must explicitly select `dex=par`.
- Use the team-wallet address for account queries.
- Signed bot actions use a separately approved API wallet.
- Never commit or log private keys, seed phrases, or API-wallet secrets.
- Keep SDK/raw API types inside `exchanges/hyperliquid/`.
- Strategy, risk, execution, and portfolio code must not import Hyperliquid SDK types.
