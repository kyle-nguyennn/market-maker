"""Offline contract tests: typed SDK wire L2 -> stateful, read-only book cache.

There are no network calls, wallets, or authenticated orders in these tests.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from exchanges.hyperliquid.config import HyperliquidVenueConfig
from exchanges.hyperliquid.types import (
    parse_l2_book_message,
    parse_l2_snapshot,
)
from market_handler.hyperliquid_market_data_handler import HyperliquidMarketDataHandler


def book(time=10_000, coin="par:BTC", bid="65000", ask="65001"):
    return {
        "coin": coin,
        "time": time,
        "levels": [
            [{"px": bid, "sz": "0.25", "n": 3}],
            [{"px": ask, "sz": "0.3", "n": 2}],
        ],
    }


def event(payload):
    return {"channel": "l2Book", "data": payload}


class FakeClient:
    def __init__(self):
        self.config = HyperliquidVenueConfig.from_environment(dex="par", environ={})
        self.rest = book()
        self.callback = None
        self.events = []
        self.subscribe_count = 0
        self.unsubscribe_count = 0
        self.on_query = None
        self.fail_rest = False

    def query_l2_book(self, instrument):
        assert instrument == "BTC"
        self.events.append(("rest", instrument))
        if self.on_query is not None:
            self.on_query()
        if self.fail_rest:
            raise RuntimeError("REST is unavailable")
        return deepcopy(self.rest)

    def subscribe_public(self, channel, instrument, callback):
        assert (channel, instrument) == ("l2Book", "BTC")
        self.callback = callback
        self.subscribe_count += 1
        return 42

    def unsubscribe_public(self, channel, instrument, subscription_id):
        assert (channel, instrument, subscription_id) == ("l2Book", "BTC", 42)
        self.unsubscribe_count += 1
        return True


@pytest.fixture
def handler():
    client = FakeClient()
    clock = [10_100]
    instance = HyperliquidMarketDataHandler(
        client, "BTC", max_age_ms=1_000, clock_ms=lambda: clock[0]
    )
    return instance, client, clock


def test_wire_rest_and_websocket_are_typed_and_structurally_checked():
    parsed = parse_l2_snapshot(book())
    assert parsed["coin"] == "par:BTC"
    assert parsed["levels"][0][0]["px"] == "65000"
    assert parse_l2_book_message(event(book()))["data"] == parsed
    assert isinstance(parsed["levels"][0][0]["sz"], str)


@pytest.mark.parametrize("payload", [
    None,
    [],
    {"coin": "", "time": 100, "levels": [[], []]},
    {"coin": "par:BTC", "time": True, "levels": [[], []]},
    {"coin": "par:BTC", "time": -1, "levels": [[], []]},
    {"coin": "par:BTC", "time": 100, "levels": [[]]},
    {"coin": "par:BTC", "time": 100, "levels": [{}, []]},
    {"coin": "par:BTC", "time": 100, "levels": [[{"px": 1, "sz": "1"}], []]},
    {"coin": "par:BTC", "time": 100, "levels": [[{"px": "1", "sz": "1", "n": False}], []]},
])
def test_malformed_sdk_l2_wire_payloads_rejected(payload):
    with pytest.raises(ValueError):
        parse_l2_snapshot(payload)


def test_wrong_ws_channel_rejected():
    with pytest.raises(ValueError, match="l2Book"):
        parse_l2_book_message({"channel": "trades", "data": book()})


def test_rest_bootstrap_has_immutable_bid_ask_state(handler):
    manager, client, clock = handler
    assert manager.current() is None
    assert not manager.ready
    state = manager.refresh()
    assert state.is_valid
    assert state.instrument == "BTC"
    assert manager.bids[0].price == Decimal("65000")
    assert manager.asks[0].price == Decimal("65001")
    assert manager.current().mid == Decimal("65000.5")
    client.rest["levels"][0][0]["px"] = "30000"
    assert manager.current() == state  # returned snapshot never aliases raw SDK data


def test_newer_ws_replaces_book_and_older_snapshot_never_regresses(handler):
    manager, _, clock = handler
    first = manager.apply_snapshot(book(), source="rest")
    newer = manager.handle_ws_message(event(book(time=10_050, bid="65002", ask="65003")))
    assert newer.best_bid == Decimal("65002")
    assert newer.timestamp_ms == 10_050
    manager.apply_snapshot(book(time=10_020, bid="10", ask="11"), source="rest")
    assert manager.current() == newer
    assert first.best_bid == Decimal("65000")  # immutable old state


def test_ws_wins_when_rest_snapshot_arrives_after_ws_during_start(handler):
    manager, client, _ = handler
    client.on_query = lambda: client.callback(event(book(
        time=10_060, bid="65010", ask="65011"
    )))
    current = manager.start()
    assert current.best_bid == Decimal("65010")
    assert current.timestamp_ms == 10_060
    assert manager.ready
    manager.stop()
    assert not manager.ready
    assert manager.current().invalid_reason == "stream_stopped"
    assert client.unsubscribe_count == 1
    client.callback(event(book(time=10_070)))  # late SDK callback after stop
    assert manager.current().timestamp_ms == 10_060


def test_equal_timestamp_rest_cannot_overwrite_ws_book(handler):
    manager, _, _ = handler
    manager.handle_ws_message(event(book(bid="65010", ask="65011")))
    manager.apply_snapshot(book(bid="65000", ask="65001"), source="rest")
    assert manager.bids[0].price == Decimal("65010")


def test_stale_book_is_invalid_without_erasing_cached_levels(handler):
    manager, _, clock = handler
    manager.apply_snapshot(book())
    clock[0] = 11_001
    state = manager.current()
    assert not state.is_valid
    assert state.invalid_reason == "stale_book"
    assert manager.bids[0].price == Decimal("65000")
    assert not manager.ready
    # Replayed older snapshots cannot reset freshness.
    manager.handle_ws_message(event(book(time=9_999)))
    assert not manager.ready
    clock[0] = 11_010
    manager.handle_ws_message(event(book(time=11_009)))
    assert manager.ready


def test_future_dated_book_quarantines_until_good_snapshot(handler):
    manager, _, _ = handler
    manager.apply_snapshot(book())
    with pytest.raises(ValueError, match="future"):
        manager.apply_snapshot(book(time=50_000))
    assert not manager.ready
    assert manager.current().invalid_reason == "future_book"
    manager.apply_snapshot(book(time=10_010))
    assert manager.ready


@pytest.mark.parametrize("replacement", [
    book(coin="pa:BTC"),
    book(coin="par:ETH"),
    book(bid="Infinity"),
    book(bid="-100"),
    book(bid="65004", ask="65003"),
    {"coin": "par:BTC", "time": 10_000, "levels": [
        [{"px": "99", "sz": "1"}, {"px": "100", "sz": "1"}],
        [{"px": "101", "sz": "1"}],
    ]},
])
def test_wrong_instrument_invalid_price_crossed_or_unsorted_book(handler, replacement):
    manager, _, _ = handler
    manager.apply_snapshot(book())
    if replacement["coin"] != "par:BTC" or replacement["levels"][0][0]["px"] in ("Infinity", "-100"):
        with pytest.raises(ValueError):
            manager.apply_snapshot(replacement)
        return
    invalid = manager.apply_snapshot(replacement)
    assert not invalid.is_valid
    assert invalid.invalid_reason in ("crossed_book", "unsorted_book")


def test_bad_ws_frame_quarantines_not_crashes_sdk_callback(handler):
    manager, client, _ = handler
    manager.start()
    client.callback({"channel": "l2Book", "data": {"garbage": True}})
    assert not manager.ready
    assert manager.current().invalid_reason == "invalid_ws_book"
    client.callback(event(book(time=10_001)))
    assert manager.ready


def test_start_failure_is_fail_closed_and_unsubscribes(handler):
    manager, client, _ = handler
    client.fail_rest = True
    with pytest.raises(RuntimeError, match="unavailable"):
        manager.start()
    assert not manager.ready
    assert client.unsubscribe_count == 1


def test_config_mismatch_and_invalid_ages_rejected(handler):
    manager, client, _ = handler
    with pytest.raises(ValueError, match="freshness"):
        HyperliquidMarketDataHandler(client, "BTC", max_age_ms=0)
    with pytest.raises(ValueError, match="dex"):
        HyperliquidMarketDataHandler(client, "pa:BTC")
    with pytest.raises(ValueError, match="DEX profile"):
        from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter
        from exchanges.hyperliquid.config import HyperliquidVenueConfig
        HyperliquidMarketDataHandler(
            client, "BTC",
            adapter=HyperliquidMarketAdapter(
                HyperliquidVenueConfig.from_environment(dex="pa", environ={})))
