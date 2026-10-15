"""Validated phase-specific Hyperliquid connection settings.

Practice DEX par: testnet/USDPR; scored DEX pa: testnet/USDP.
Verified 2026-10-15: pa is on testnet (3 symbols visible), not mainnet.
Secrets belong in local environment variables, never in this module.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


_PROFILES = {
    "par": {
        "network": "testnet",
        "api_url": "https://api.hyperliquid-testnet.xyz",
        "ws_url": "wss://api.hyperliquid-testnet.xyz/ws",
        "dex": "par",
        "collateral": "USDPR",
    },
    "pa": {
        "network": "testnet",
        "api_url": "https://api.hyperliquid-testnet.xyz",
        "ws_url": "wss://api.hyperliquid-testnet.xyz/ws",
        "dex": "pa",
        "collateral": "USDP",
    },
}
_ALLOWED_PHASES = frozenset(_PROFILES)


@dataclass(frozen=True)
class HyperliquidVenueConfig:
    """One internally consistent market endpoint and collateral profile.

    A mismatched DEX, network, URL or collateral asset is rejected before any
    SDK client is constructed. Selecting pa does not authorize signed trading.
    """

    phase: str = "par"
    network: str = "testnet"
    api_url: str = "https://api.hyperliquid-testnet.xyz"
    ws_url: str = "wss://api.hyperliquid-testnet.xyz/ws"
    dex: str = "par"
    collateral: str = "USDPR"
    allow_live_trading: bool = False
    sdk_version: str = "0.24.0"

    def __post_init__(self) -> None:
        if self.phase not in _ALLOWED_PHASES:
            raise ValueError("phase must be par (practice) or pa (scored)")
        expected = _PROFILES[self.phase]
        for field, value in expected.items():
            actual = getattr(self, field)
            if field == "api_url":
                actual = actual.rstrip("/")
            if actual != value:
                raise ValueError(
                    f"{self.phase}: invalid {field} ({actual!r}); "
                    f"expected {value!r}. Check network/dex/collateral configuration"
                )
        if self.sdk_version != "0.24.0":
            raise ValueError("supported Hyperliquid SDK version is 0.24.0")
        if self.phase == "par" and self.allow_live_trading:
            raise ValueError("live trading authorization is only valid for pa")

    @classmethod
    def from_environment(
        cls,
        *,
        phase: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "HyperliquidVenueConfig":
        """Load .env and process variables, or an explicit mapping in tests.

        python-dotenv preserves existing process variables (override=False).
        Explicit phase= (e.g. --phase pa) takes precedence over the env selector.
        Values for the other DEX are never consulted.
        """
        if environ is None:
            from dotenv import load_dotenv

            load_dotenv(override=False)
            environ = os.environ
        selected = (phase or environ.get("PERPETUAL_ALPHA_PHASE", "par")).strip().lower()
        if selected not in _ALLOWED_PHASES:
            raise ValueError("PERPETUAL_ALPHA_PHASE must be par or pa")
        defaults = _PROFILES[selected]
        prefix = f"PERPETUAL_ALPHA_{selected.upper()}_"
        settings = {
            key: environ.get(prefix + key.upper(), value).strip()
            for key, value in defaults.items()
        }
        raw_allow = environ.get("PERPETUAL_ALPHA_PA_ALLOW_TRADING", "false").strip().lower()
        if raw_allow not in {"true", "false", "1", "0"}:
            raise ValueError("PERPETUAL_ALPHA_PA_ALLOW_TRADING must be true or false")
        allow_live = selected == "pa" and raw_allow in {"true", "1"}
        return cls(phase=selected, allow_live_trading=allow_live, **settings)

    def venue_symbol(self, instrument: str) -> str:
        if not instrument or not instrument.strip():
            raise ValueError("instrument must be non-empty")
        if ":" in instrument:
            if not instrument.startswith(f"{self.dex}:") or instrument == f"{self.dex}:":
                raise ValueError(f"instrument must belong to dex {self.dex}")
            return instrument
        return f"{self.dex}:{instrument}"
