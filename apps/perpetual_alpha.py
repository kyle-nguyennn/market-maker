"""Perpetual Alpha read-only diagnostics for either competition DEX.

python -m apps.perpetual_alpha --check                   # profile in .env
python -m apps.perpetual_alpha --check --phase par       # practice testnet
python -m apps.perpetual_alpha --check --phase pa        # scored mainnet
Never signs orders or reads an API private key.
"""
from __future__ import annotations

import argparse
import os

from exchanges.hyperliquid.client import HyperliquidVenueClient
from exchanges.hyperliquid.config import HyperliquidVenueConfig
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter


def build_read_only_stack(
    phase: str | None = None,
) -> tuple[HyperliquidVenueClient, HyperliquidMarketAdapter]:
    """The same validated profile must drive both SDK and symbol adapter."""
    config = HyperliquidVenueConfig.from_environment(phase=phase)
    return HyperliquidVenueClient(config), HyperliquidMarketAdapter(config)


def check_market(
    client: HyperliquidVenueClient,
    adapter: HyperliquidMarketAdapter,
    team_wallet: str,
    instrument: str,
) -> None:
    """Read selected DEX market/book/account; no Exchange signer is initialized."""
    coin = adapter.to_venue_instrument(instrument)
    meta = client.query_meta()
    markets = {market["name"]: market for market in meta["universe"]}
    if coin not in markets:
        raise RuntimeError(f"{coin} is not present in {client.config.dex} metadata")

    book = client.query_l2_book(instrument)
    contexts = client.query_meta_and_asset_contexts()
    ctx_names = [item["name"] for item in contexts[0]["universe"]]
    if coin not in ctx_names:
        raise RuntimeError(f"{coin} missing from asset contexts on {client.config.dex}")
    context = contexts[1][ctx_names.index(coin)]
    market_state = adapter.from_sdk_l2_snapshot(book, asset_ctx=context)

    account = client.query_account_state(team_wallet)
    spot_balances = account["spot"].get("balances", [])
    collateral = next(
        (entry for entry in spot_balances if entry.get("coin") == client.config.collateral),
        None,
    )
    positions = account["perps"].get("assetPositions", [])

    print(
        f"Network: {client.config.network}; API: {client.config.api_url}; "
        f"DEX: {client.config.dex}; market: {coin}"
    )
    print(f"Market size decimals: {markets[coin]['szDecimals']}")
    print(f"L2 best bid / ask: {market_state.best_bid} / {market_state.best_ask}")
    print(f"Internal MarketState valid: {market_state.is_valid}")
    print(f"Oracle / mark: {context.get('oraclePx')} / {context.get('markPx')}")
    print(f"{client.config.collateral} spot collateral: {collateral}")
    print(
        f"Open {client.config.dex} positions / orders: "
        f"{len(positions)} / {len(account['open_orders'])}"
    )
    if collateral is None:
        print(
            f"WARNING: {client.config.collateral} is absent from the team wallet's "
            "spot balances. Market connectivity does not imply account funding."
        )
    print("Read-only checks complete; no trading action was sent.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Perpetual Alpha read-only venue diagnostics")
    parser.add_argument("--check", action="store_true", help="query the selected network")
    parser.add_argument(
        "--phase", choices=("par", "pa"), default=None,
        help="override PERPETUAL_ALPHA_PHASE from .env without changing it",
    )
    parser.add_argument("--instrument", default="BTC", help="unqualified market e.g. BTC")
    args = parser.parse_args()
    client, adapter = build_read_only_stack(phase=args.phase)
    if not args.check:
        print(
            f"SDK configured: network={client.config.network} "
            f"dex={client.config.dex} collateral={client.config.collateral}. "
            "Use --check for read-only venue queries."
        )
        return

    # Config loading already populated the environment from the local .env.
    team_wallet = os.environ.get("PERPETUAL_ALPHA_TEAM_WALLET")
    if not team_wallet:
        parser.error("set PERPETUAL_ALPHA_TEAM_WALLET in the local .env")
    try:
        check_market(client, adapter, team_wallet, args.instrument)
    finally:
        client.close_stream()


if __name__ == "__main__":
    main()
