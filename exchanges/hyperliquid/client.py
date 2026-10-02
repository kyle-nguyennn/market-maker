from __future__ import annotations

from typing import Any

from .config import HyperliquidVenueConfig
from .types import VenueOrderRequest, VenueOrderResult


class HyperliquidVenueClient:
    """Thin wrapper around the official Hyperliquid SDK.

    The client accepts and returns Hyperliquid-facing objects only. Internal
    Decimal domain objects are converted by HyperliquidMarketAdapter before
    entering or after leaving this boundary.

    Signed actions remain disabled until the team wallet is funded and the
    separate API wallet has been approved.
    """

    def __init__(self, config: HyperliquidVenueConfig | None = None) -> None:
        self.config = config or HyperliquidVenueConfig()
        self._info: Any | None = None
        self._exchange: Any | None = None

    def connect_info(self) -> None:
        """Initialize the SDK Info client."""
        raise NotImplementedError("wire official Hyperliquid Info client")

    def query_meta(self) -> Any:
        """Return metadata for the configured dex."""
        raise NotImplementedError("query meta with dex='par'")

    def query_meta_and_asset_contexts(self) -> Any:
        """Return mark/oracle/funding/open-interest context for the dex."""
        raise NotImplementedError("query metaAndAssetCtxs with dex='par'")

    def query_l2_book(self, instrument: str) -> Any:
        """Return the current raw L2 snapshot for one prefixed market."""
        _ = self.config.venue_symbol(instrument)
        raise NotImplementedError("query l2Book for prefixed coin")

    def query_account_state(self, team_wallet: str) -> Any:
        """Read raw positions/orders/balance using the TEAM wallet address."""
        _ = team_wallet
        raise NotImplementedError("query unified account state")

    def connect_exchange(self, *, team_wallet: str, api_private_key: str) -> None:
        """Initialize signed trading actions using the approved API wallet."""
        _ = (team_wallet, api_private_key)
        raise NotImplementedError("wire official Hyperliquid Exchange client")

    def place_order(self, request: VenueOrderRequest) -> VenueOrderResult:
        """Place one venue-formatted order and return the raw venue result."""
        _ = request
        raise NotImplementedError("signed trading is not scaffolded yet")
