"""Contract for the published Scalar SDK. No API requests or credentials."""
from __future__ import annotations

import importlib.util

import pytest


@pytest.mark.skipif(
    importlib.util.find_spec("super_market") is None,
    reason="SIG optional dependency not installed in core/Hyperliquid environment",
)
def test_super_market_sdk_public_interface_and_retry_guard():
    from super_market import AsyncSuperMarketAPI, SuperMarketAPI
    from super_market._version import __version__

    assert __version__ == "0.1.0"
    # Production order dispatch must opt out of SDK implicit retries.
    sdk = SuperMarketAPI(
        bearer_auth="test-not-a-real-key",
        max_retries=0,
        timeout=10.0,
        base_url="https://www.thesuper.market/api/v1",
    )
    try:
        assert sdk.max_retries == 0
        assert hasattr(sdk.orders, "create")
        assert hasattr(sdk.orders, "delete")
        assert hasattr(sdk.exchanges, "list_orderbook")
        assert hasattr(sdk.account, "list")
        assert AsyncSuperMarketAPI is not None
    finally:
        sdk.close()
