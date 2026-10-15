"""Offline contract tests for the pinned Hyperliquid SDK 0.24.0 boundary.

Mocks emulate SDK calls; these tests NEVER access testnet or send real orders.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from exchanges.hyperliquid import client as venue_client
from exchanges.hyperliquid.client import HyperliquidVenueClient, _sdk_float
from exchanges.hyperliquid.config import HyperliquidVenueConfig
from exchanges.hyperliquid.types import OrderSide, TimeInForce, VenueOrderRequest

TEAM = "0x" + "a" * 40
AGENT = "0x" + "b" * 40
CLOID = "0x" + "c" * 32


@pytest.fixture
def sdk(monkeypatch):
    class FakeCloid:
        def __init__(self, raw):
            self.raw = raw

        @classmethod
        def from_str(cls, raw):
            return cls(raw)

    class FakeInfo:
        instances = []

        def __init__(self, base_url, skip_ws, perp_dexs):
            self.base_url = base_url
            self.skip_ws = skip_ws
            self.perp_dexs = perp_dexs
            self.calls = []
            self.subscriptions = []
            FakeInfo.instances.append(self)

        def meta(self, dex=""):
            self.calls.append(("meta", dex))
            return {"universe": [{"name": "par:BTC", "szDecimals": 4}]}

        def post(self, path, body):
            self.calls.append(("post", path, body))
            return {"type": body["type"], "dex": body["dex"]}

        def all_mids(self, dex=""):
            self.calls.append(("all_mids", dex))
            return {"par:BTC": "65000"}

        def l2_snapshot(self, coin):
            self.calls.append(("l2", coin))
            return {"coin": coin, "time": 4, "levels": [[{"px": "65000", "sz": "1"}],
                                                          [{"px": "65001", "sz": "1"}]]}

        def spot_user_state(self, address):
            self.calls.append(("spot", address))
            return {"balances": [{"coin": "USDPR", "total": "1000000"}]}

        def user_state(self, address, dex=""):
            self.calls.append(("positions", address, dex))
            return {"assetPositions": []}

        def open_orders(self, address, dex=""):
            self.calls.append(("orders", address, dex))
            return []

        def user_fills(self, address):
            self.calls.append(("fills", address))
            return [{"coin": "BTC"}, {"coin": "par:BTC"}]

        def query_order_by_oid(self, address, oid):
            self.calls.append(("status_oid", address, oid))
            return {"status": "open"}

        def query_order_by_cloid(self, address, cloid):
            self.calls.append(("status_cloid", address, cloid.raw))
            return {"status": "open"}

        def subscribe(self, subscription, callback):
            self.subscriptions.append((subscription, callback))
            return len(self.subscriptions)

        def disconnect_websocket(self):
            self.calls.append(("close",))

    class FakeExchange:
        instances = []

        def __init__(self, wallet, base_url, account_address, perp_dexs):
            self.wallet = wallet
            self.base_url = base_url
            self.account_address = account_address
            self.perp_dexs = perp_dexs
            self.calls = []
            self.next_order = {"status": "ok", "response": {"data": {"statuses": [
                {"resting": {"oid": 42}}]}}}
            self.next_cancel = {"status": "ok", "response": {"data": {"statuses": ["success"]}}}
            FakeExchange.instances.append(self)

        def order(self, *args, **kwargs):
            self.calls.append(("order", args, kwargs))
            return self.next_order

        def cancel(self, symbol, oid):
            self.calls.append(("cancel", symbol, oid))
            return self.next_cancel

        def cancel_by_cloid(self, symbol, cloid):
            self.calls.append(("cancel_cloid", symbol, cloid.raw))
            return self.next_cancel

        def update_leverage(self, leverage, symbol, is_cross):
            self.calls.append(("leverage", leverage, symbol, is_cross))
            return {"status": "ok"}

    monkeypatch.setattr(venue_client, "Info", FakeInfo)
    monkeypatch.setattr(venue_client, "Exchange", FakeExchange)
    monkeypatch.setattr(venue_client, "Cloid", FakeCloid)
    monkeypatch.setattr(venue_client, "Account", SimpleNamespace(
        from_key=lambda key: SimpleNamespace(address=AGENT)
    ))
    return FakeInfo, FakeExchange


def make_order(**changes):
    data = {
        "instrument": "par:BTC",
        "side": OrderSide.BUY,
        "price": "65000",
        "size": "0.0002",
        "tif": TimeInForce.ALO,
        "client_order_id": CLOID,
    }
    data.update(changes)
    return VenueOrderRequest(**data)


def test_queries_restrict_market_scope_and_collateral(sdk):
    FakeInfo, _ = sdk
    client = HyperliquidVenueClient()
    assert client.query_meta()["universe"][0]["szDecimals"] == 4
    assert client.query_meta_and_asset_contexts() == {"type": "metaAndAssetCtxs", "dex": "par"}
    assert client.query_all_mids()["par:BTC"] == "65000"
    assert client.query_l2_book("BTC")["coin"] == "par:BTC"
    assert client.query_account_state(TEAM)["spot"]["balances"][0]["coin"] == "USDPR"
    assert client.query_fills(TEAM) == [{"coin": "par:BTC"}]
    info = FakeInfo.instances[0]
    assert info.skip_ws is True
    assert info.perp_dexs == ["par"]
    assert ("post", "/info", {"type": "metaAndAssetCtxs", "dex": "par"}) in info.calls
    assert ("positions", TEAM, "par") in info.calls
    assert ("orders", TEAM, "par") in info.calls


def test_order_status_lookup_oid_and_cloid(sdk):
    client = HyperliquidVenueClient()
    client.query_order_status(TEAM, order_id=12)
    client.query_order_status(TEAM, client_order_id=CLOID)
    info = sdk[0].instances[0]
    assert ("status_oid", TEAM, 12) in info.calls
    assert ("status_cloid", TEAM, CLOID) in info.calls


def test_trading_requires_explicit_agent(sdk):
    client = HyperliquidVenueClient()
    with pytest.raises(RuntimeError, match="connect"):
        client.place_order(make_order())
    with pytest.raises(RuntimeError, match="connect"):
        client.cancel_order("BTC", order_id=42)


def test_signed_order_uses_agent_and_parent_account(sdk):
    _, FakeExchange = sdk
    client = HyperliquidVenueClient()
    client.connect_exchange(team_wallet=TEAM, api_private_key="0xagentkey")
    exchange = FakeExchange.instances[-1]
    assert exchange.wallet.address == AGENT
    assert exchange.account_address == TEAM
    assert exchange.perp_dexs == ["par"]
    result = client.place_order(make_order())
    assert result.status == "resting"
    assert result.order_id == 42
    op, args, kwargs = exchange.calls[-1]
    assert op == "order"
    assert args == ("par:BTC", True, 0.0002, 65000.0, {"limit": {"tif": "Alo"}})
    assert kwargs["cloid"].raw == CLOID
    assert kwargs["reduce_only"] is False


@pytest.mark.parametrize(
    "status,result",
    [
        ({"resting": {"oid": 321}}, ("resting", 321, None)),
        ({"filled": {"oid": 13, "totalSz": "0.0002", "avgPx": "65000"}},
         ("filled", 13, "0.0002")),
        ({"error": "post only would match"}, ("error", None, None)),
    ],
)
def test_every_order_status_is_parsed(sdk, status, result):
    client = HyperliquidVenueClient()
    client.connect_exchange(team_wallet=TEAM, api_private_key="0xagentkey")
    exchange = sdk[1].instances[-1]
    exchange.next_order = {"status": "ok", "response": {"data": {"statuses": [status]}}}
    answer = client.place_order(make_order())
    assert (answer.status, answer.order_id, answer.filled_size) == result


@pytest.mark.parametrize("response", [
    {"status": "err", "response": "bad signature"},
    {"status": "ok", "response": {"data": {"statuses": []}}},
    {"status": "ok", "response": {"data": {"statuses": [{}, {}]}}},
    {"status": "ok", "response": {"data": {"statuses": [{"unexpected": {}}]}}},
])
def test_order_rejects_bad_nested_status(sdk, response):
    client = HyperliquidVenueClient()
    client.connect_exchange(team_wallet=TEAM, api_private_key="0xagentkey")
    sdk[1].instances[-1].next_order = response
    with pytest.raises(RuntimeError):
        client.place_order(make_order())


def test_cancel_uses_oid_or_cloid_and_checks_nested_result(sdk):
    client = HyperliquidVenueClient()
    client.connect_exchange(team_wallet=TEAM, api_private_key="0xagentkey")
    exchange = sdk[1].instances[-1]
    assert client.cancel_order("BTC", order_id=42)
    assert client.cancel_order("BTC", client_order_id=CLOID)
    assert ("cancel", "par:BTC", 42) in exchange.calls
    assert ("cancel_cloid", "par:BTC", CLOID) in exchange.calls
    exchange.next_cancel = {"status": "ok", "response": {"data": {"statuses": [
        {"error": "unknown oid"}]}}}
    with pytest.raises(RuntimeError, match="unknown oid"):
        client.cancel_order("BTC", order_id=42)


def test_market_scope_and_credentials_are_hard_guarded(sdk, monkeypatch):
    with pytest.raises(ValueError, match="testnet"):
        HyperliquidVenueClient(HyperliquidVenueConfig(api_url="https://api.hyperliquid.xyz"))
    with pytest.raises(ValueError, match="invalid dex"):
        HyperliquidVenueClient(HyperliquidVenueConfig(dex="xyz"))
    client = HyperliquidVenueClient()
    with pytest.raises(ValueError, match="team_wallet"):
        client.connect_exchange(team_wallet="bad", api_private_key="secret")
    with pytest.raises(ValueError, match="team's master"):
        # Set fake agent address to team address before authorizing.
        monkeypatch.setattr(venue_client, "Account", SimpleNamespace(
            from_key=lambda _: SimpleNamespace(address=TEAM)
        ))
        client.connect_exchange(team_wallet=TEAM, api_private_key="secret")


def test_invalid_client_id_and_precision_do_not_reach_exchange(sdk):
    client = HyperliquidVenueClient()
    client.connect_exchange(team_wallet=TEAM, api_private_key="secret")
    with pytest.raises(ValueError, match="client_order_id"):
        client.place_order(make_order(client_order_id="not-a-cloid"))
    with pytest.raises(ValueError, match="8 decimals"):
        client.place_order(make_order(size="0.000000001"))
    with pytest.raises(ValueError, match="positive"):
        client.place_order(make_order(price="0"))
    assert sdk[1].instances[-1].calls == []


def test_sdk_float_roundtrip_and_nonfinite_rejected():
    assert _sdk_float("0.00000001") == 1e-8
    assert _sdk_float("65000.001") == 65000.001
    for bad in ["NaN", "Infinity", "-1", "0", "0.000000001"]:
        with pytest.raises(ValueError):
            _sdk_float(bad)


def test_realtime_subscriptions_use_prefixed_coin_and_parent_address(sdk):
    FakeInfo, _ = sdk
    client = HyperliquidVenueClient()
    events = []
    assert client.subscribe_public("l2Book", "BTC", events.append) == 1
    assert client.subscribe_private("orderUpdates", TEAM, events.append) == 2
    info = FakeInfo.instances[0]
    assert info.skip_ws is False
    assert info.subscriptions[0][0] == {"type": "l2Book", "coin": "par:BTC"}
    assert info.subscriptions[1][0] == {"type": "orderUpdates", "user": TEAM}
    client.close_stream()
    assert ("close",) in info.calls


def test_no_unscoped_or_unauthorized_ws_channel(sdk):
    client = HyperliquidVenueClient()
    with pytest.raises(ValueError):
        client.subscribe_public("allMids", "BTC", lambda x: None)
    with pytest.raises(ValueError):
        client.subscribe_private("l2Book", TEAM, lambda x: None)


def test_leverage_is_explicit_and_uses_prefixed_market(sdk):
    client = HyperliquidVenueClient()
    client.connect_exchange(team_wallet=TEAM, api_private_key="secret")
    with pytest.raises(ValueError):
        client.update_leverage("BTC", 0)
    client.update_leverage("BTC", 2)
    assert ("leverage", 2, "par:BTC", True) in sdk[1].instances[-1].calls
