# Hyperliquid adapter notes

- Use Hyperliquid **testnet** only for Perpetual Alpha.
- The competition DEX is `par`; market names are `par:BTC`, `par:ETH`, etc.
- Market-wide and position queries must explicitly select `dex=par`.
- Use the team-wallet address for account queries.
- Signed bot actions use a separately approved API wallet.
- Never commit or log private keys, seed phrases, or API-wallet secrets.
- Keep SDK/raw API types inside `exchanges/hyperliquid/`.
- Strategy code consumes normalized domain state/events from `market_handler/`.
