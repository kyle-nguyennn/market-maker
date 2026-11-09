"""Alpaca equities market-data adapter (historical and live minute bars).

The optional alpaca-py SDK is only imported inside factory functions. This
module never imports alpaca.trading and never submits orders. Both adapters
produce alpha.observations.PriceBar and are independent of FeatureEngine.
"""
from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from alpha.observations import PriceBar

MINUTE_MS = 60_000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FEEDS = frozenset({"iex", "sip"})
_ADJUSTMENTS = frozenset({"raw", "split", "dividend", "all"})


def _sdk_error() -> RuntimeError:
    return RuntimeError(
        "Alpaca stock-data adapter requires alpaca-py; "
        "install with: pip install -r requirements-alpaca.txt"
    )


def _credentials_from_environment() -> tuple[str, str]:
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise ValueError(
            "Set ALPACA_API_KEY and ALPACA_SECRET_KEY (or APCA_API_KEY_ID "
            "and APCA_API_SECRET_KEY) for market-data access"
        )
    return key, secret


def _validate_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
        raise ValueError("stock symbol must be a nonempty trimmed string")
    return symbol


def _validate_ms(ms: int, label: str) -> int:
    if type(ms) is not int or ms < 0:
        raise ValueError(f"{label} must be a nonnegative epoch millisecond integer")
    return ms


def _validate_feed(feed: str) -> str:
    if feed not in _FEEDS:
        raise ValueError("stock feed must be 'iex' or 'sip'")
    return feed


def _to_ms(timestamp: datetime) -> int:
    """Convert only timezone-aware Alpaca bar-start timestamps, no local TZ."""
    if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
        raise ValueError("Alpaca bar timestamp must be timezone-aware")
    delta = timestamp.astimezone(timezone.utc) - _EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _from_ms(epoch_ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=_validate_ms(epoch_ms, "epoch_ms"))


def _decimal(value: Any, field: str) -> Decimal:
    """Preserve a numeric string; SDK float input can already be rounded."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError(f"invalid {field} value from Alpaca")
    try:
        parsed = Decimal(str(value))
    except (ValueError, ArithmeticError) as exc:
        raise ValueError(f"invalid {field} value from Alpaca") from exc
    if not parsed.is_finite():
        raise ValueError(f"nonfinite {field} value from Alpaca")
    return parsed


def normalize_alpaca_stock_bar(
    sdk_bar: Any, *, source: str, available_at_ms: int,
) -> PriceBar:
    """Convert an SDK Bar (opening timestamp) into a completed 1-minute bar.

    Do not infer publication time from the exchange timestamp. For live data
    pass the locally measured callback receipt time. For historical bars pass
    an *explicit assumed* delay policy; historical APIs cannot establish the
    exact first publication/revision availability of a past observation.
    """
    _validate_ms(available_at_ms, "available_at_ms")
    symbol = _validate_symbol(sdk_bar.symbol)
    start_ms = _to_ms(sdk_bar.timestamp)
    end_ms = start_ms + MINUTE_MS
    if available_at_ms < end_ms:
        raise ValueError("Alpaca bar cannot be used before its interval closes")
    return PriceBar(
        symbol=symbol,
        end_ms=end_ms,
        available_at_ms=available_at_ms,
        interval_ms=MINUTE_MS,
        source=source,
        open=_decimal(sdk_bar.open, "open"),
        high=_decimal(sdk_bar.high, "high"),
        low=_decimal(sdk_bar.low, "low"),
        close=_decimal(sdk_bar.close, "close"),
        volume=_decimal(sdk_bar.volume, "volume"),
    )


def _historical_client(key: str, secret: str) -> Any:
    try:
        from alpaca.data.historical import StockHistoricalDataClient
    except ImportError as exc:
        raise _sdk_error() from exc
    return StockHistoricalDataClient(key, secret)


def _stock_bars_request(
    symbol: str, start_ms: int, end_ms: int, *, feed: str, adjustment: str,
) -> Any:
    try:
        from alpaca.data.enums import Adjustment, DataFeed, Sort
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except ImportError as exc:
        raise _sdk_error() from exc
    return StockBarsRequest(
        symbol_or_symbols=symbol,
        start=_from_ms(start_ms), end=_from_ms(end_ms),
        timeframe=TimeFrame.Minute, feed=DataFeed(feed),
        adjustment=Adjustment(adjustment), sort=Sort.ASC,
    )


def _stock_stream(key: str, secret: str, *, feed: str) -> Any:
    try:
        from alpaca.data.enums import DataFeed
        from alpaca.data.live import StockDataStream
    except ImportError as exc:
        raise _sdk_error() from exc
    return StockDataStream(key, secret, feed=DataFeed(feed))


class AlpacaHistoricalBarProvider:
    """Read-only SDK stock bar provider with an explicit research clock policy.

    Historical Alpaca bars are *reconstructed*, not an original publication
    log: available_at_ms=end_ms+assumed_publication_lag_ms is an explicit model
    assumption, NOT recorded live latency. Adjusted OHLCV may incorporate
    corporate actions known only later; data is NOT PIT-certified. Only use
    the output for exploratory/cost-aware replay after checking vendor
    adjustment, revision, symbol and session semantics.

    The lookback start is configured explicitly to satisfy BarProvider's
    get_bars(symbol, until_ms=...) contract. No silent unbounded history.
    """

    def __init__(
        self, *, start_ms: int, assumed_publication_lag_ms: int,
        source_version: str, feed: str = "iex", adjustment: str = "raw",
        client: Any | None = None,
        api_key: str | None = None, secret_key: str | None = None,
    ) -> None:
        _validate_ms(start_ms, "start_ms")
        _validate_ms(assumed_publication_lag_ms, "assumed_publication_lag_ms")
        _validate_feed(feed)
        if adjustment not in _ADJUSTMENTS:
            raise ValueError("unknown Alpaca corporate-action adjustment")
        if not isinstance(source_version, str) or not source_version.strip():
            raise ValueError("explicit source_version required for provenance")
        if (api_key is None) != (secret_key is None):
            raise ValueError("api_key and secret_key must be supplied together")
        if client is None and not (api_key and secret_key):
            raise ValueError("inject a data-only SDK client or API credentials")
        self.start_ms = start_ms
        self.assumed_publication_lag_ms = assumed_publication_lag_ms
        self.feed = feed
        self.adjustment = adjustment
        self.source = (
            f"alpaca:stocks:{feed}:{adjustment}:1m:"
            f"historical-assumed-lag={assumed_publication_lag_ms}ms:"
            f"{source_version.strip()}"
        )
        self._client = client if client is not None else _historical_client(
            api_key, secret_key
        )

    @classmethod
    def from_environment(cls, **kwargs: Any) -> "AlpacaHistoricalBarProvider":
        key, secret = _credentials_from_environment()
        return cls(api_key=key, secret_key=secret, **kwargs)

    def get_bars(self, symbol: str, *, until_ms: int) -> tuple[PriceBar, ...]:
        """Return as-of-eligible minute bars; no interpolation or filling."""
        _validate_symbol(symbol)
        _validate_ms(until_ms, "until_ms")
        if until_ms <= self.start_ms:
            raise ValueError("until_ms must be after configured start_ms")
        request = _stock_bars_request(
            symbol, self.start_ms, until_ms,
            feed=self.feed, adjustment=self.adjustment,
        )
        response = self._client.get_stock_bars(request)
        data = getattr(response, "data", None)
        if not isinstance(data, Mapping):
            raise TypeError("expected Alpaca BarSet.data mapping")
        records = data.get(symbol, [])
        bars = []
        for item in records:
            if _validate_symbol(item.symbol) != symbol:
                raise ValueError("Alpaca returned a different symbol")
            end_ms = _to_ms(item.timestamp) + MINUTE_MS
            if end_ms > until_ms:
                # Even if the endpoint returned a forming interval, ignore it.
                continue
            available_ms = end_ms + self.assumed_publication_lag_ms
            if available_ms > until_ms:
                continue
            bars.append(normalize_alpaca_stock_bar(
                item, source=self.source, available_at_ms=available_ms,
            ))
        bars.sort(key=lambda b: b.end_ms)
        if any(a.end_ms == b.end_ms for a, b in zip(bars, bars[1:])):
            raise ValueError("historical Alpaca response contains duplicate minute bars")
        return tuple(bars)


class AlpacaMinuteBarStream:
    """Live SDK minute bar subscriber; no trading SDK and no implicit run().

    subscribe() registers an asynchronous callback with StockDataStream.
    run() explicitly starts the SDK's blocking WebSocket loop. The callback
    records local UTC receipt time as availability time and forwards the
    normalized bar to on_bar(bar, at_ms). This avoids conflating bar-end time
    with observed receipt time. Source is fixed and cannot be silently
    mixed with historical reconstructed availability/revisions.
    """

    def __init__(
        self, *, symbols: tuple[str, ...],
        on_bar: Callable[[PriceBar, int], Awaitable[None]],
        feed: str = "iex", source_version: str = "live-v1",
        stream: Any | None = None,
        clock_ms: Callable[[], int] | None = None,
        api_key: str | None = None, secret_key: str | None = None,
    ) -> None:
        if not isinstance(symbols, tuple) or not symbols:
            raise ValueError("symbols must be a nonempty tuple")
        for symbol in symbols:
            _validate_symbol(symbol)
        if len(set(symbols)) != len(symbols):
            raise ValueError("duplicate stream symbols")
        if not callable(on_bar):
            raise TypeError("on_bar must be an async callable")
        _validate_feed(feed)
        if not isinstance(source_version, str) or not source_version.strip():
            raise ValueError("source_version required")
        if (api_key is None) != (secret_key is None):
            raise ValueError("api_key and secret_key must be supplied together")
        if stream is None and not (api_key and secret_key):
            raise ValueError("inject StockDataStream or data API credentials")
        self.symbols = symbols
        self.source = f"alpaca:stocks:{feed}:raw:1m:live:{source_version.strip()}"
        self._on_bar = on_bar
        self._stream = stream if stream is not None else _stock_stream(
            api_key, secret_key, feed=feed
        )
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._subscribed = False

    @classmethod
    def from_environment(cls, **kwargs: Any) -> "AlpacaMinuteBarStream":
        key, secret = _credentials_from_environment()
        return cls(api_key=key, secret_key=secret, **kwargs)

    async def _handle(self, sdk_bar: Any) -> None:
        at_ms = _validate_ms(self._clock_ms(), "callback receipt time")
        normalized = normalize_alpaca_stock_bar(
            sdk_bar, source=self.source, available_at_ms=at_ms,
        )
        if normalized.symbol not in self.symbols:
            raise ValueError("bar belongs to an unsubscribed symbol")
        await self._on_bar(normalized, at_ms)

    def subscribe(self) -> None:
        """Register handlers only; SDK WebSocket isn't connected until run()."""
        if self._subscribed:
            raise RuntimeError("already subscribed to live Alpaca bars")
        self._stream.subscribe_bars(self._handle, *self.symbols)
        self._subscribed = True

    def run(self) -> None:
        """Explicitly start the SDK's blocking WebSocket event loop."""
        if not self._subscribed:
            raise RuntimeError("call subscribe() before run()")
        self._stream.run()

    def stop(self) -> None:
        """Request the SDK live stream to stop (no trading operations)."""
        self._stream.stop()
