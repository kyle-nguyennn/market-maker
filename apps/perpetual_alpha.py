from __future__ import annotations

from exchanges.hyperliquid.client import HyperliquidVenueClient
from exchanges.hyperliquid.config import HyperliquidVenueConfig
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter


def build_read_only_stack() -> tuple[
    HyperliquidVenueClient,
    HyperliquidMarketAdapter,
]:
    """Build the pre-funding Perpetual Alpha stack.

    The current scaffold is intentionally read-only. Signed trading is wired
    only after the organizers fund the team wallet and an API wallet is
    approved.
    """
    config = HyperliquidVenueConfig()
    return HyperliquidVenueClient(config), HyperliquidMarketAdapter(config)


def main() -> None:
    client, adapter = build_read_only_stack()
    print(
        "Perpetual Alpha scaffold ready:",
        f"dex={client.config.dex}",
        f"sample_market={adapter.instrument('BTC')}",
        "mode=read-only",
    )


if __name__ == "__main__":
    main()
