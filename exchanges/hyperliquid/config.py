"""Validated DEX-specific Hyperliquid connection settings.

Practice DEX par: testnet/USDPR; scored DEX pas: testnet/USDP.
The official participant guide specifies scored dex pas (index 271).
Previous pa metadata probes on 2026-10-15 did not validate scored pas.
Secrets belong in local environment variables, never in this module.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


_DEX_PROFILES = {
    "par": {
        "network": "testnet",
        "api_url": "https://api.hyperliquid-testnet.xyz",
        "ws_url": "wss://api.hyperliquid-testnet.xyz/ws",
        "collateral": "USDPR",
    },
    "pas": {
        "network": "testnet",
        "api_url": "https://api.hyperliquid-testnet.xyz",
        "ws_url": "wss://api.hyperliquid-testnet.xyz/ws",
        "collateral": "USDP",
    },
}
_ALLOWED_DEXES = frozenset(_DEX_PROFILES)


@dataclass(frozen=True)
class HyperliquidVenueConfig:
    """One internally consistent market endpoint and collateral profile.

    A mismatched DEX, network, URL or collateral asset is rejected before any
    SDK client is constructed. Selecting pas does not authorize signed trading.
    """

    network: str = "testnet"
    api_url: str = "https://api.hyperliquid-testnet.xyz"
    ws_url: str = "wss://api.hyperliquid-testnet.xyz/ws"
    dex: str = "par"
    collateral: str = "USDPR"
    allow_live_trading: bool = False
    sdk_version: str = "0.24.0"

    def __post_init__(self) -> None:
        if self.dex not in _ALLOWED_DEXES:
            raise ValueError("dex must be par (practice) or pas (scored)")
        expected = _DEX_PROFILES[self.dex]
        for field, value in expected.items():
            actual = getattr(self, field)
            if field == "api_url":
                actual = actual.rstrip("/")
            if actual != value:
                raise ValueError(
                    f"{self.dex}: invalid {field} ({actual!r}); "
                    f"expected {value!r}. Check network/dex/collateral configuration"
                )
        if self.sdk_version != "0.24.0":
            raise ValueError("supported Hyperliquid SDK version is 0.24.0")
        if self.dex == "par" and self.allow_live_trading:
            raise ValueError("live trading authorization is only valid for pas")

    @classmethod
    def from_environment(
        cls,
        *,
        dex: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "HyperliquidVenueConfig":
        """Load .env and process variables, or an explicit mapping in tests.

        python-dotenv preserves existing process variables (override=False).
        Explicit dex= (e.g. --dex pas) takes precedence over the env selector.
        Values for the other DEX are never consulted.
        """
        if environ is None:
            from dotenv import load_dotenv

            load_dotenv(override=False)
            environ = os.environ
        selected = (dex or environ.get("PERPETUAL_ALPHA_DEX", "par")).strip().lower()
        if selected not in _ALLOWED_DEXES:
            raise ValueError("PERPETUAL_ALPHA_DEX must be par or pas")
        defaults = _DEX_PROFILES[selected]
        prefix = f"PERPETUAL_ALPHA_{selected.upper()}_"
        settings = {
            key: environ.get(prefix + key.upper(), value).strip()
            for key, value in defaults.items()
        }
        raw_allow = environ.get("PERPETUAL_ALPHA_PAS_ALLOW_TRADING", "false").strip().lower()
        if raw_allow not in {"true", "false", "1", "0"}:
            raise ValueError("PERPETUAL_ALPHA_PAS_ALLOW_TRADING must be true or false")
        allow_live = selected == "pas" and raw_allow in {"true", "1"}
        return cls(dex=selected, allow_live_trading=allow_live, **settings)

    def venue_symbol(self, instrument: str) -> str:
        if not instrument or not instrument.strip():
            raise ValueError("instrument must be non-empty")
        if ":" in instrument:
            if not instrument.startswith(f"{self.dex}:") or instrument == f"{self.dex}:":
                raise ValueError(f"instrument must belong to dex {self.dex}")
            return instrument
        return f"{self.dex}:{instrument}"
