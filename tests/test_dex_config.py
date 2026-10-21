"""Environment selection, endpoint isolation and signed-action safety tests."""
from __future__ import annotations

import pytest

from apps.perpetual_alpha import build_read_only_stack
from exchanges.hyperliquid.client import HyperliquidVenueClient
from exchanges.hyperliquid.config import HyperliquidVenueConfig
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter


def _environment(dex: str) -> dict[str, str]:
    return {
        "PERPETUAL_ALPHA_DEX": dex,
        "PERPETUAL_ALPHA_PAR_NETWORK": "testnet",
        "PERPETUAL_ALPHA_PAR_API_URL": "https://api.hyperliquid-testnet.xyz",
        "PERPETUAL_ALPHA_PAR_WS_URL": "wss://api.hyperliquid-testnet.xyz/ws",
        "PERPETUAL_ALPHA_PAR_COLLATERAL": "USDPR",
        "PERPETUAL_ALPHA_PAS_NETWORK": "testnet",
        "PERPETUAL_ALPHA_PAS_API_URL": "https://api.hyperliquid-testnet.xyz",
        "PERPETUAL_ALPHA_PAS_WS_URL": "wss://api.hyperliquid-testnet.xyz/ws",
        "PERPETUAL_ALPHA_PAS_COLLATERAL": "USDP",
        "PERPETUAL_ALPHA_PAS_ALLOW_TRADING": "false",
    }


@pytest.mark.parametrize(
    "dex,network,url,ws,collateral,expected_symbol",
    [
        ("par", "testnet", "https://api.hyperliquid-testnet.xyz",
         "wss://api.hyperliquid-testnet.xyz/ws", "USDPR", "par:BTC"),
        ("pas", "testnet", "https://api.hyperliquid-testnet.xyz",
         "wss://api.hyperliquid-testnet.xyz/ws", "USDP", "pas:BTC"),
    ],
)
def test_dex_resolves_all_routing_fields(
    dex, network, url, ws, collateral, expected_symbol
):
    config = HyperliquidVenueConfig.from_environment(environ=_environment(dex))
    assert config.dex == dex
    assert config.network == network
    assert config.api_url == url
    assert config.ws_url == ws
    assert config.collateral == collateral
    assert config.venue_symbol("BTC") == expected_symbol
    assert not config.allow_live_trading



def test_only_dex_identifies_the_selected_market():
    from dataclasses import fields

    config = HyperliquidVenueConfig.from_environment(environ=_environment("pas"))
    names = {field.name for field in fields(config)}
    assert "dex" in names
    assert "phase" not in names
    assert config.dex == "pas"


def test_explicit_dex_overrides_environment_selector():
    config = HyperliquidVenueConfig.from_environment(
        dex="pas", environ=_environment("par")
    )
    assert config.dex == "pas" and config.collateral == "USDP"


@pytest.mark.parametrize(
    "key,value",
    [
        ("PERPETUAL_ALPHA_PAS_NETWORK", "mainnet"),
        ("PERPETUAL_ALPHA_PAS_API_URL", "https://api.hyperliquid.xyz"),
        ("PERPETUAL_ALPHA_PAS_WS_URL", "wss://api.hyperliquid.xyz/ws"),
        ("PERPETUAL_ALPHA_PAS_COLLATERAL", "USDPR"),
    ],
)
def test_live_profile_rejects_cross_wired_fields(key, value):
    data = _environment("pas")
    data[key] = value
    with pytest.raises(ValueError, match="invalid"):
        HyperliquidVenueConfig.from_environment(environ=data)


def test_wrong_dex_and_invalid_trading_ack_fail_closed():
    with pytest.raises(ValueError, match="must be par or pas"):
        HyperliquidVenueConfig.from_environment(environ={"PERPETUAL_ALPHA_DEX": "other"})
    data = _environment("pas")
    data["PERPETUAL_ALPHA_PAS_ALLOW_TRADING"] = "yes!"
    with pytest.raises(ValueError, match="must be true or false"):
        HyperliquidVenueConfig.from_environment(environ=data)


def test_live_signer_disabled_even_with_private_key(monkeypatch):
    data = _environment("pas")
    config = HyperliquidVenueConfig.from_environment(environ=data)
    client = HyperliquidVenueClient(config)
    with pytest.raises(PermissionError, match="disabled"):
        client.connect_exchange(team_wallet="0x" + "a" * 40, api_private_key="secret")


def test_adapter_refuses_wrong_dex_symbols():
    config = HyperliquidVenueConfig.from_environment(environ=_environment("pas"))
    adapter = HyperliquidMarketAdapter(config)
    assert adapter.to_internal_instrument("pas:ETH") == "ETH"
    assert adapter.to_venue_instrument("ETH") == "pas:ETH"
    with pytest.raises(ValueError, match="dex pas"):
        adapter.to_venue_instrument("par:ETH")
    with pytest.raises(ValueError, match="dex pas"):
        adapter.to_internal_instrument("par:ETH")


def test_selected_endpoint_routes_to_both_rest_and_ws(monkeypatch):
    constructed = []
    class FakeInfo:
        def __init__(self, api_url, skip_ws, perp_dexs):
            constructed.append((api_url, skip_ws, perp_dexs))
        def meta(self, dex=""):
            assert dex == "pas"
            return {"universe": [{"name": "pas:BTC", "szDecimals": 4}]}
        def subscribe(self, subscription, callback):
            assert subscription == {"type": "l2Book", "coin": "pas:BTC"}
            return 7
        def disconnect_websocket(self):
            pass

    monkeypatch.setattr("exchanges.hyperliquid.client.Info", FakeInfo)
    config = HyperliquidVenueConfig.from_environment(environ=_environment("pas"))
    client = HyperliquidVenueClient(config)
    assert client.query_meta()["universe"][0]["name"] == "pas:BTC"
    assert client.subscribe_public("l2Book", "BTC", lambda _: None) == 7
    assert constructed == [
        ("https://api.hyperliquid-testnet.xyz", True, ["pas"]),
        ("https://api.hyperliquid-testnet.xyz", False, ["pas"]),
    ]
    client.close_stream()


def test_live_signed_sdk_uses_testnet_and_scored_dex(monkeypatch):
    data = _environment("pas")
    data["PERPETUAL_ALPHA_PAS_ALLOW_TRADING"] = "true"
    config = HyperliquidVenueConfig.from_environment(environ=data)
    assert config.allow_live_trading
    captured = {}
    class FakeExchange:
        def __init__(self, account, base_url, account_address, perp_dexs):
            captured.update(
                wallet=account, api_url=base_url, team=account_address,
                dexes=perp_dexs
            )

    fake_agent = type("Agent", (), {"address": "0x" + "b" * 40})()
    monkeypatch.setattr("exchanges.hyperliquid.client.Account.from_key", lambda _: fake_agent)
    monkeypatch.setattr("exchanges.hyperliquid.client.Exchange", FakeExchange)
    client = HyperliquidVenueClient(config)
    client.connect_exchange(team_wallet="0x" + "a" * 40, api_private_key="dummy-test-key")
    assert captured["api_url"] == "https://api.hyperliquid-testnet.xyz"
    assert captured["dexes"] == ["pas"]
    assert captured["team"] == "0x" + "a" * 40


def test_app_builds_single_shared_profile(monkeypatch):
    env = _environment("par")
    env["PERPETUAL_ALPHA_DEX"] = "pas"
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    client, adapter = build_read_only_stack()
    assert client.config is adapter.config
    assert client.config.dex == "pas"
    client2, adapter2 = build_read_only_stack("par")
    assert client2.config is adapter2.config
    assert client2.config.dex == "par"


def test_par_ignores_pa_live_ack_and_keeps_testnet(monkeypatch):
    env = _environment("par")
    env["PERPETUAL_ALPHA_PAS_ALLOW_TRADING"] = "true"
    profile = HyperliquidVenueConfig.from_environment(environ=env)
    assert profile.api_url == "https://api.hyperliquid-testnet.xyz"
    assert not profile.allow_live_trading


def test_obsolete_pa_selector_is_rejected_not_routed_to_scored_dex():
    data = _environment("pas")
    data["PERPETUAL_ALPHA_DEX"] = "pa"
    with pytest.raises(ValueError, match="must be par or pas"):
        HyperliquidVenueConfig.from_environment(environ=data)


def test_legacy_pa_trading_flag_never_authorizes_pas():
    data = _environment("pas")
    data["PERPETUAL_ALPHA_PA_ALLOW_TRADING"] = "true"  # deprecated; ignored
    config = HyperliquidVenueConfig.from_environment(environ=data)
    assert not config.allow_live_trading
    with pytest.raises(ValueError, match="dex pas"):
        config.venue_symbol("pa:BTC")
