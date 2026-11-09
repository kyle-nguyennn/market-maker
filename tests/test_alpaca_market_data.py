"""Offline contract tests of Alpaca stock-bar ingestion; never contact Alpaca."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from alpha import FeatureConfig, FeatureEngine, PriceBar
from market_handler import alpaca_stock_market_data as mod

UTC = timezone.utc
BASE = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)


def sdk_bar(minute=0, *, symbol="AAPL", ts=None, **changes):
    fields = dict(
        symbol=symbol,
        timestamp=BASE + timedelta(minutes=minute) if ts is None else ts,
        open=100.5,
        high=101.0,
        low=100.0,
        close=100.75,
        volume=123,
    )
    fields.update(changes)
    return SimpleNamespace(**fields)


def unix_ms(dt):
    return int(dt.timestamp() * 1000)


def test_bar_normalization_sdk_opening_time_to_completed_end_and_decimal():
    source = "alpaca:iex:raw:live:test"
    end = unix_ms(BASE) + mod.MINUTE_MS
    normalized = mod.normalize_alpaca_stock_bar(
        sdk_bar(), source=source, available_at_ms=end + 1200,
    )
    assert isinstance(normalized, PriceBar)
    assert normalized.symbol == "AAPL"
    assert normalized.end_ms == end
    assert normalized.available_at_ms == end + 1200
    assert normalized.interval_ms == 60000
    assert normalized.source == source
    assert normalized.open == D("100.5")
    assert normalized.close == D("100.75")
    assert normalized.volume == D("123")
    assert all(isinstance(v, D) for v in (
        normalized.open, normalized.high, normalized.low,
        normalized.close, normalized.volume,
    ))


def test_price_normalization_rejects_future_early_naive_nonfinite():
    end = unix_ms(BASE) + 60000
    with pytest.raises(ValueError, match="before its interval closes"):
        mod.normalize_alpaca_stock_bar(
            sdk_bar(), source="alpaca:test", available_at_ms=end - 1,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        mod.normalize_alpaca_stock_bar(
            sdk_bar(ts=BASE.replace(tzinfo=None)),
            source="alpaca:test", available_at_ms=end + 100,
        )
    with pytest.raises(ValueError, match="nonfinite"):
        mod.normalize_alpaca_stock_bar(
            sdk_bar(close=float("inf")), source="alpaca:test",
            available_at_ms=end + 100,
        )
    with pytest.raises(ValueError, match="invalid"):
        mod.normalize_alpaca_stock_bar(
            sdk_bar(open=True), source="alpaca:test",
            available_at_ms=end + 100,
        )
    with pytest.raises(ValueError, match="nonnegative"):
        mod.normalize_alpaca_stock_bar(
            sdk_bar(), source="alpaca:test", available_at_ms=True,
        )


class StubHistory:
    def __init__(self, items):
        self.items = list(items)
        self.request = None

    def get_stock_bars(self, request):
        self.request = request
        return SimpleNamespace(data={"AAPL": self.items})


def test_historical_provider_uses_explicit_publication_assumption(monkeypatch):
    client = StubHistory([sdk_bar(i) for i in [2, 0, 1, 3]])
    captured = {}
    def request_builder(symbol, start_ms, end_ms, *, feed, adjustment):
        captured.update(dict(symbol=symbol, start_ms=start_ms, end_ms=end_ms,
                             feed=feed, adjustment=adjustment))
        return object()
    monkeypatch.setattr(mod, "_stock_bars_request", request_builder)

    base = unix_ms(BASE)
    provider = mod.AlpacaHistoricalBarProvider(
        start_ms=base, assumed_publication_lag_ms=1000,
        source_version="fixture-20261109", client=client,
    )
    output = provider.get_bars("AAPL", until_ms=base + 3 * 60000 + 1000)
    assert [b.end_ms for b in output] == [
        base + 60000, base + 120000, base + 180000,
    ]
    assert all(b.available_at_ms == b.end_ms + 1000 for b in output)
    assert len(output) == 3  # fourth bar is not completed/available yet
    assert "historical-assumed-lag=1000ms" in output[0].source
    assert "fixture-20261109" in output[0].source
    assert captured == dict(
        symbol="AAPL", start_ms=base, end_ms=base + 181000,
        feed="iex", adjustment="raw",
    )
    assert client.request is not None
    assert output[0].close == D("100.75")


def test_historical_provider_withholds_bar_until_assumed_publication(monkeypatch):
    monkeypatch.setattr(mod, "_stock_bars_request", lambda *args, **kw: object())
    base = unix_ms(BASE)
    client = StubHistory([sdk_bar(0)])
    provider = mod.AlpacaHistoricalBarProvider(
        start_ms=base, assumed_publication_lag_ms=5000,
        source_version="asof-v1", client=client,
    )
    assert provider.get_bars("AAPL", until_ms=base + 64000) == ()
    assert len(provider.get_bars("AAPL", until_ms=base + 65000)) == 1


def test_historical_provider_fails_on_duplicate_or_cross_symbol(monkeypatch):
    monkeypatch.setattr(mod, "_stock_bars_request", lambda *args, **kw: object())
    base = unix_ms(BASE)
    client = StubHistory([sdk_bar(), sdk_bar()])
    provider = mod.AlpacaHistoricalBarProvider(
        start_ms=base, assumed_publication_lag_ms=0,
        source_version="x", client=client,
    )
    with pytest.raises(ValueError, match="duplicate"):
        provider.get_bars("AAPL", until_ms=base + 70000)
    client.items = [sdk_bar(symbol="MSFT")]
    with pytest.raises(ValueError, match="different symbol"):
        provider.get_bars("AAPL", until_ms=base + 70000)


def test_historical_config_requires_explicit_time_policy_and_source():
    base = unix_ms(BASE)
    for changes in [
        dict(assumed_publication_lag_ms=-1),
        dict(assumed_publication_lag_ms=True),
        dict(feed="unknown"),
        dict(adjustment="wrong"),
        dict(source_version=" "),
        dict(start_ms=True),
    ]:
        kwargs = dict(start_ms=base, assumed_publication_lag_ms=0,
                      source_version="fixture", client=StubHistory([]))
        kwargs.update(changes)
        with pytest.raises(ValueError):
            mod.AlpacaHistoricalBarProvider(**kwargs)
    with pytest.raises(ValueError, match="client or API credentials"):
        mod.AlpacaHistoricalBarProvider(
            start_ms=base, assumed_publication_lag_ms=0,
            source_version="v1",
        )


class StubStream:
    def __init__(self):
        self.handler = None
        self.symbols = None
        self.running = False
        self.stopped = False

    def subscribe_bars(self, handler, *symbols):
        self.handler = handler
        self.symbols = symbols

    def run(self):
        self.running = True

    def stop(self):
        self.stopped = True


def test_live_stream_async_callback_preserves_receipt_time_and_adapter_contract():
    received = []
    base = unix_ms(BASE)
    sdk = StubStream()
    async def on_bar(price_bar, at_ms):
        received.append((price_bar, at_ms))

    adapter = mod.AlpacaMinuteBarStream(
        symbols=("AAPL",), on_bar=on_bar, stream=sdk,
        clock_ms=lambda: base + 62000,
    )
    with pytest.raises(RuntimeError, match="subscribe"):
        adapter.run()
    adapter.subscribe()
    assert sdk.symbols == ("AAPL",)
    with pytest.raises(RuntimeError, match="already"):
        adapter.subscribe()
    asyncio.run(sdk.handler(sdk_bar()))
    assert len(received) == 1
    observation, arrival = received[0]
    assert observation.end_ms == base + 60000
    assert observation.available_at_ms == arrival == base + 62000
    assert observation.source == "alpaca:stocks:iex:raw:1m:live:live-v1"
    adapter.run()
    assert sdk.running
    adapter.stop()
    assert sdk.stopped


def test_live_stream_rejects_wrong_symbol_and_unclosed_bar():
    sdk = StubStream()
    events = []
    async def on_bar(price_bar, at_ms):
        events.append(price_bar)
    base = unix_ms(BASE)
    adapter = mod.AlpacaMinuteBarStream(
        symbols=("AAPL",), on_bar=on_bar, stream=sdk,
        clock_ms=lambda: base + 62000,
    )
    adapter.subscribe()
    with pytest.raises(ValueError, match="unsubscribed"):
        asyncio.run(sdk.handler(sdk_bar(symbol="MSFT")))
    assert events == []
    early = mod.AlpacaMinuteBarStream(
        symbols=("AAPL",), on_bar=on_bar, stream=StubStream(),
        clock_ms=lambda: base + 59000,
    )
    early.subscribe()
    with pytest.raises(ValueError, match="before its interval closes"):
        asyncio.run(early._handle(sdk_bar()))
    assert events == []


def test_live_normalized_events_feed_feature_engine_without_sdk_in_alpha():
    base = unix_ms(BASE)
    emitted = []
    clock = {"now": base}
    sdk = StubStream()
    engine = FeatureEngine(
        symbol="AAPL",
        config=FeatureConfig(
            interval_ms=60000, lookback_bars=3,
            max_staleness_ms=3000, feature_version="alpaca-price-v1",
        ),
    )
    async def publish(bar, at_ms):
        emitted.append(engine.on_bar(bar, at_ms=at_ms))
    adapter = mod.AlpacaMinuteBarStream(
        symbols=("AAPL",), stream=sdk, on_bar=publish,
        clock_ms=lambda: clock["now"],
    )
    adapter.subscribe()
    for minute in range(4):
        clock["now"] = base + (minute + 1) * 60000 + 1000
        asyncio.run(sdk.handler(sdk_bar(minute, close=float(101 + minute),
                                        high=float(102 + minute),
                                        low=100.0)))
    assert emitted[:3] == [None, None, None]
    assert emitted[3] is not None
    assert emitted[3].values["momentum"] > 0


def test_historical_and_live_source_ids_are_distinct(monkeypatch):
    base = unix_ms(BASE)
    monkeypatch.setattr(mod, "_stock_bars_request", lambda *a, **k: object())
    hist = mod.AlpacaHistoricalBarProvider(
        start_ms=base, assumed_publication_lag_ms=1000,
        source_version="history-v1", client=StubHistory([sdk_bar()]),
    )
    live = mod.AlpacaMinuteBarStream(
        symbols=("AAPL",), on_bar=lambda *args: None, stream=StubStream(),
    )
    assert hist.source != live.source


def test_environment_keys_are_resolved_only_on_explicit_construction(monkeypatch):
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY",
                "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        mod._credentials_from_environment()
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    assert mod._credentials_from_environment() == ("test-key", "test-secret")
    # No outbound requests or SDK constructors are used by the mock tests.


def test_alpaca_module_is_data_only_and_does_not_import_trading_code():
    from pathlib import Path
    code = Path(mod.__file__).read_text(encoding="utf-8")
    assert "from alpaca.trading" not in code
    assert "from market_handler" not in code  # no feed coupling
    import alpha
    assert "alpaca" not in alpha.__dict__


def test_official_alpaca_sdk_request_contract_if_dependency_installed():
    """SDK smoke test executes in optional CI with pinned alpaca-py installed."""
    pytest.importorskip("alpaca.data.requests")
    from alpaca.common.enums import Sort
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    start = unix_ms(BASE)
    request = mod._stock_bars_request(
        "AAPL", start, start + 180000, feed="iex", adjustment="raw",
    )
    assert isinstance(request, StockBarsRequest)
    assert request.symbol_or_symbols == "AAPL"
    assert request.feed == DataFeed.IEX
    assert request.adjustment == Adjustment.RAW
    assert request.sort == Sort.ASC
    assert request.timeframe.amount == TimeFrame.Minute.amount
    assert request.timeframe.unit == TimeFrame.Minute.unit
    assert request.start == BASE
    assert request.end == BASE + timedelta(minutes=3)
