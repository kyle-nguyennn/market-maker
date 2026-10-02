from __future__ import annotations

from typing import Any

from .config import HyperliquidVenueConfig
from .types import VenueOrderRequest, VenueOrderResult


class HyperliquidVenueClient:
    """Thin wrapper around the official Hyperliquid SDK.\n\n    This client accepts only venue-formatted request objects. Internal Decimal\n    OrderIntent objects must be converted by HyperliquidMarketAdapter first.

    This file intentionally starts read-only. Signed actions are added only
    after the team wallet has been funded and the separate API wallet has
    been approved.

    Keep all SDK-specific objects behind this boundary.
    """

    def __init__(self, config: HyperliquidVenueConfig | None = None) -> None:
        self.config = config or HyperliquidVenueConfig()
        self._info: Any | None = None
        self._exchange: Any | None = None

    def connect_info(self) -> None:
        """Initialize the SDK Info client.

        TODO:
        - import the official hyperliquid-python-sdk
        - point it at config.api_url
        - select config.dex through perp_dexs
        - enable WebSocket support when subscriptions are implemented
        """
        raise NotImplementedError("wire official Hyperliquid Info client")

    def query_meta(self) -> Any:
        """Return metadata for the configured dex."""
        raise NotImplementedError("query meta with dex='par'")

    def query_meta_and_asset_contexts(self) -> Any:
        """Return mark/oracle/funding/open-interest context for the dex."""
        raise NotImplementedError("query metaAndAssetCtxs with dex='par'")

    def query_l2_book(self, instrument: str) -> Any:
        """Return the current L2 snapshot for one prefixed market."""
        _ = self.config.venue_symbol(instrument)
        raise NotImplementedError("query l2Book for prefixed coin")

    def query_account_state(self, team_wallet: str) -> Any:
        """Read positions/orders/balance using the TEAM wallet address."""
        _ = team_wallet
        raise NotImplementedError("query unified account state")

    def connect_exchange(self, *, team_wallet: str, api_private_key: str) -> None:
        """Initialize signed trading actions using the approved API wallet.

        Never pass the team-wallet private key into the trading bot.
        """
        _ = (team_wallet, api_private_key)
        raise NotImplementedError("wire official Hyperliquid Exchange client")

    def place_order(self, request: VenueOrderRequest) -> VenueOrderResult:
        """Place one order and normalize the per-operation result.

        A request-level status of 'ok' is not sufficient; inspect the
        individual response.data.statuses entry.
        """
        _ = request
        raise NotImplementedError("signed trading is not scaffolded yet")
