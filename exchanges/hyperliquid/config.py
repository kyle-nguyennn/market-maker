from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HyperliquidVenueConfig:
    """Venue settings for Perpetual Alpha.

    Values mirror the current participant guide. Secrets and wallet keys do
    not belong here; load them from the runtime environment/secret store.
    """

    api_url: str = "https://api.hyperliquid-testnet.xyz"
    ws_url: str = "wss://api.hyperliquid-testnet.xyz/ws"
    dex: str = "par"
    collateral: str = "USDPR"
    sdk_version: str = "0.24.0"

    def venue_symbol(self, instrument: str) -> str:
        if not instrument or not instrument.strip():
            raise ValueError("instrument must be non-empty")
        if ":" in instrument:
            if not instrument.startswith(f"{self.dex}:") or instrument == f"{self.dex}:":
                raise ValueError(f"instrument must belong to dex {self.dex}")
            return instrument
        return f"{self.dex}:{instrument}"
