"""Offline tests for the first par order/cancel smoke test; no network or keys."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import time

import pytest

from apps.par_first_order import (
    HARD_MAX_NOTIONAL,
    MIN_ORDER_NOTIONAL,
    SmokeTestError,
    _assert_after,
    prepare_order,
    run_once,
)
from exchanges.hyperliquid.config import HyperliquidVenueConfig
from exchanges.hyperliquid.types import TimeInForce, VenueOrderResult
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter


WALLET = "0x" + "a" * 40
CLOID = "0x" + "c" * 32
NOW = int(time.time() * 1000)


class FakeClient:
    def __init__(self):
        self.config = HyperliquidVenueConfig()
        self.book_time = NOW - 100
        self.bids = [{"px": "81000", "sz": "2"}]
        self.asks = [{"px": "81010", "sz": "2"}]
        self.positions = []
        self.open_orders = []
        self.balances = [{"coin": "USDPR", "total": "1000000", "hold": "0"}]
        self.order_response = VenueOrderResult("resting", order_id=321)
        self.signed = False
        self.sent = None
        self.cancels = []
        self.cancel_exception = None
        self.post_exception = None

    def query_meta(self):
        return {"universe": [{"name": "par:BTC", "szDecimals": 4}]}

    def query_l2_book(self, instrument):
        assert instrument == "BTC"
        return {
            "coin": "par:BTC", "time": self.book_time,
            "levels": [self.bids, self.asks],
        }

    def query_open_orders(self, wallet):
        assert wallet == WALLET
        return deepcopy(self.open_orders)

    def query_positions(self, wallet):
        assert wallet == WALLET
        return {"assetPositions": deepcopy(self.positions)}

    def query_spot_balance(self, wallet):
        assert wallet == WALLET
        return {"balances": deepcopy(self.balances)}

    def connect_exchange(self, *, team_wallet, api_private_key):
        assert team_wallet == WALLET
        assert api_private_key == "test-agent-key"
        self.signed = True

    def place_order(self, request):
        assert self.signed
        self.sent = request
        self.open_orders = [{
            "coin": "par:BTC", "cloid": request.client_order_id, "oid": 321
        }]
        if self.post_exception:
            raise self.post_exception
        return self.order_response

    def cancel_order(self, instrument, *, client_order_id):
        assert instrument == "BTC"
        self.cancels.append(client_order_id)
        if self.cancel_exception:
            raise self.cancel_exception
        self.open_orders = [
            o for o in self.open_orders
            if o.get("cloid") != client_order_id
        ]
        return True


@pytest.fixture
def adapter():
    return HyperliquidMarketAdapter(HyperliquidVenueConfig())


@pytest.fixture
def client():
    return FakeClient()


def plan(client, adapter):
    return prepare_order(
        client, adapter, WALLET, now_ms=NOW, client_order_id=CLOID
    )


def test_dry_run_plans_lot_quantized_passive_alo_order(client, adapter):
    p = plan(client, adapter)
    assert p.request.instrument == "par:BTC"
    assert p.request.client_order_id == CLOID
    assert p.request.tif is TimeInForce.ALO
    assert p.request.side.value == "BUY"
    assert p.request.reduce_only is False
    assert Decimal(p.request.price) < p.bid < p.ask
    assert Decimal(p.request.size) % Decimal("0.0001") == 0
    assert MIN_ORDER_NOTIONAL <= p.notional <= HARD_MAX_NOTIONAL
    assert client.signed is False and client.sent is None


@pytest.mark.parametrize(
    "problem",
    [
        "stale", "future", "crossed", "missing_ask", "old_order",
        "position", "collateral", "bad_notional_precision",
    ],
)
def test_preflight_fails_without_signed_order(client, adapter, problem):
    if problem == "stale":
        client.book_time = NOW - 6000
    elif problem == "future":
        client.book_time = NOW + 1000
    elif problem == "crossed":
        client.asks = [{"px": "80999", "sz": "2"}]
    elif problem == "missing_ask":
        client.asks = []
    elif problem == "old_order":
        client.open_orders = [{"coin": "par:BTC", "oid": 9}]
    elif problem == "position":
        client.positions = [{"position": {"coin": "par:BTC", "szi": "0.2"}}]
    elif problem == "collateral":
        client.balances = [{"coin": "USDPR", "total": "9", "hold": "0"}]
    elif problem == "bad_notional_precision":
        client.query_meta = lambda: {
            "universe": [{"name": "par:BTC", "szDecimals": 9}]
        }
    with pytest.raises(SmokeTestError):
        plan(client, adapter)
    assert not client.signed
    assert client.sent is None


def test_preflight_blocks_non_practice_venue(client, adapter):
    client.config = object.__new__(HyperliquidVenueConfig)
    object.__setattr__(client.config, "dex", "pa")
    with pytest.raises(SmokeTestError, match="only testnet par"):
        plan(client, adapter)


def test_one_resting_order_cancel_and_verify_flat(client, adapter):
    run_once(client, adapter, WALLET, "test-agent-key", wait_seconds=0)
    assert client.signed
    assert client.sent.instrument == "par:BTC"
    assert client.cancels == [client.sent.client_order_id]
    assert client.open_orders == []


def test_ambiguous_placement_timeout_attempts_cancel_by_cloid(client, adapter):
    client.post_exception = TimeoutError("request timed out")
    with pytest.raises(TimeoutError):
        run_once(client, adapter, WALLET, "test-agent-key", wait_seconds=0)
    assert client.sent
    assert client.cancels == [client.sent.client_order_id]
    assert client.open_orders == []


def test_unexpected_alo_fill_attempts_cancel_and_reports_issue(client, adapter):
    client.order_response = VenueOrderResult(
        "filled", order_id=321, filled_size="0.0002", average_price="81000"
    )
    with pytest.raises(SmokeTestError, match="expected post-only"):
        run_once(client, adapter, WALLET, "test-agent-key", wait_seconds=0)
    assert client.cancels


def test_cancel_exception_cannot_be_silently_ignored(client, adapter):
    client.cancel_exception = TimeoutError("cancel timed out")
    with pytest.raises(TimeoutError, match="cancel timed out"):
        run_once(client, adapter, WALLET, "test-agent-key", wait_seconds=0)
    assert client.open_orders != []


def test_verification_rejects_residual_position_and_open_order(client):
    client.positions = [
        {"position": {"coin": "par:BTC", "szi": "-0.0002"}}
    ]
    with pytest.raises(SmokeTestError, match="position is"):
        _assert_after(client, WALLET, CLOID)
    client.positions = []
    client.open_orders = [{"coin": "par:BTC", "cloid": CLOID}]
    with pytest.raises(SmokeTestError, match="still open"):
        _assert_after(client, WALLET, CLOID)
