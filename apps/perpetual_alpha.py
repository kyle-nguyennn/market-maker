"""Perpetual Alpha testnet diagnostics.

Read-only by design. There are no order-placement CLI commands here.
Run 'python -m apps.perpetual_alpha --check' after installing the SDK.
"""
from __future__ import annotations

import argparse
import os

from exchanges.hyperliquid.client import HyperliquidVenueClient
from exchanges.hyperliquid.config import HyperliquidVenueConfig
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter


def build_read_only_stack() -> tuple[HyperliquidVenueClient, HyperliquidMarketAdapter]:
    config = HyperliquidVenueConfig()
    return HyperliquidVenueClient(config), HyperliquidMarketAdapter(config)


def check_testnet(client: HyperliquidVenueClient, adapter: HyperliquidMarketAdapter,
                  team_wallet: str, instrument: str) -> None:
    """Query real testnet SDK endpoints, without constructing an Exchange signer."""
    coin = adapter.to_venue_instrument(instrument)
    meta = client.query_meta()
    markets = {market["name"]: market for market in meta["universe"]}
    if coin not in markets:
        raise RuntimeError(f"{coin} not present in par metadata")

    book = client.query_l2_book(instrument)
    levels = book["levels"]
    bid = levels[0][0]["px"] if levels[0] else None
    ask = levels[1][0]["px"] if levels[1] else None
    ctxs = client.query_meta_and_asset_contexts()
    ctx_names = [item["name"] for item in ctxs[0]["universe"]]
    context = ctxs[1][ctx_names.index(coin)]

    account = client.query_account_state(team_wallet)
    spot_balances = account["spot"].get("balances", [])
    collateral = next(
        (entry for entry in spot_balances if entry.get("coin") == client.config.collateral),
        None,
    )
    positions = account["perps"].get("assetPositions", [])

    print(f"Network: testnet; dex: {client.config.dex}; market: {coin}")
    print(f"Market size decimals: {markets[coin]['szDecimals']}")
    print(f"L2 best bid / ask: {bid} / {ask}")
    print(f"Oracle / mark: {context.get('oraclePx')} / {context.get('markPx')}")
    print(f"{client.config.collateral} spot collateral: {collateral}")
    print(f"Open par positions / orders: {len(positions)} / {len(account['open_orders'])}")
    print("Read-only checks complete; no trading action was sent.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Perpetual Alpha testnet read-only diagnostics")
    parser.add_argument("--check", action="store_true", help="query actual testnet data")
    parser.add_argument("--instrument", default="BTC", help="par market, e.g. BTC")
    args = parser.parse_args()

    client, adapter = build_read_only_stack()
    if not args.check:
        print("SDK adapter configured; run with --check for read-only testnet queries.")
        return

    team_wallet = os.environ.get("PERPETUAL_ALPHA_TEAM_WALLET")
    if not team_wallet:
        parser.error("set PERPETUAL_ALPHA_TEAM_WALLET before --check")

    try:
        check_testnet(client, adapter, team_wallet, args.instrument)
    finally:
        client.close_stream()


if __name__ == "__main__":
    main()
