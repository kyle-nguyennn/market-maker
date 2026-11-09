# Alpaca US-equities market-data adapter (SDK only; no trading)

This host-side market-data adapter uses the official Python SDK
[`alpaca-py`](https://github.com/alpacahq/alpaca-py) to retrieve historical
equities OHLCV and subscribe to live IEX/SIP minute bars. It **never** constructs
an Alpaca TradingClient, submits orders, or connects to AlphArena. The vendored
`alpha/` package remains independently publishable.

## Installation

```bash
python -m pip install -r requirements/alpaca.txt
```

The dependency is optional and currently pinned to `alpaca-py==0.45.0`.
Existing Hyperliquid dependencies are unaffected.

Obtain market-data-capable API credentials from the
[Alpaca dashboard](https://app.alpaca.markets/), then export
`ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (or SDK-compatible
`APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`). Keep credentials in your
local secret manager/environment. **Do not add them to tracked files.**

## Fetch historical minute bars (read only)

```python
from datetime import datetime, timezone
from market_handler.alpaca_stock_market_data import AlpacaHistoricalBarProvider
from alpha import FeatureConfig, FeatureEngine, replay_bars

def utc_ms(year, month, day):
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)

source = AlpacaHistoricalBarProvider.from_environment(
    start_ms=utc_ms(2026, 11, 2),
    assumed_publication_lag_ms=1500,  # EXPLICIT hypothesis, not measured
    source_version="research-snapshot-2026-11-09",
    feed="iex",       # SIP may need different entitlement
    adjustment="raw", # no back-adjusted dividend/split future leakage
)
bars = source.get_bars("AAPL", until_ms=utc_ms(2026, 11, 7))
engine = FeatureEngine(
    symbol="AAPL", config=FeatureConfig(
        interval_ms=60_000, lookback_bars=20,
        max_staleness_ms=120_000, feature_version="price-v1",
    ),
)
snapshots = replay_bars(bars, engine)
```

**Point-in-time limitation:** Historical Alpaca `Bar` objects provide the
bar's *opening time* and OHLCV; they do **not** provide the original bar's first
publication time, receipt latency, or the revision record as it existed then.
This adapter requires `assumed_publication_lag_ms` and includes it in the
`PriceBar.source` identity. This is a declared modeling assumption rather
than a measured historical truth. For production-quality leakage testing,
capture live receipt timestamps and revisions in a separate event journal.

The `source_version` argument identifies **your data capture/research
snapshot**, not a provider-guaranteed immutable Alpaca dataset revision.
Changing corporate-action adjustment, IEX/SIP feed or delay policy changes
the source identity. Always record these parameters in experimental results.

## Subscribe to live minute bars (explicit network operation)

```python
from market_handler.alpaca_stock_market_data import AlpacaMinuteBarStream

# The strategy-pipeline instance must be created separately, with its own
# FeatureEngine, SignalEngine and Strategy. A paper position is NOT submitted.
async def handle_bar(bar, received_at_ms):
    result = pipeline.on_bar(bar, at_ms=received_at_ms)
    if result is not None:
        print("Latest signal count:", len(result.signals))

feed = AlpacaMinuteBarStream.from_environment(
    symbols=("AAPL",),
    on_bar=handle_bar,
    feed="iex",
    source_version="ws-capture-v1",
)
feed.subscribe()  # register SDK handlers; no connection yet
feed.run()        # EXPLICIT blocking connection; run in own process/thread
# On shutdown: feed.stop()
```

Alpaca sends a minute bar with its **opening timestamp**; the adapter converts
that to `PriceBar.end_ms = open_timestamp + 60_000` and sets
`available_at_ms` to **local UTC callback receipt time**. If an incomplete
or early bar arrives, the conversion fails closed. Only normal completed-minute
bars are subscribed. The SDK supports `subscribe_updated_bars`, but this
adapter intentionally does not subscribe to corrections: a historical
revision/correction changes a previously emitted bar and requires a separately
versioned revision-policy/reconciliation API. The regular live stream
`on_bar()` is single-event, in-order and single-owner; cross-thread fan-out
and automatic reconnect/failure recovery are not implemented here.

Live and historical source IDs are deliberately **different**. The existing
`FeatureEngine` rejects mixing source IDs within one rolling engine;
replay-to-live warmup must use an explicit reconciliation/policy, not silently
merge measured and assumed publication times.

## Known limitations and research obligations

- Alpaca SDK bar properties are generally `float` values.
  `Decimal(str(value))` avoids further binary float arithmetic but **cannot
  restore precision already lost** by the SDK. For tick-critical applications,
  a raw wire-data decoder would be required.
- Minute bars can be absent during no-trade periods, halts and equity-session
  closures. The current `FeatureEngine` treats gaps as missing data and
  requires fresh warmup. P2-B must implement exchange-calendar-aware
  normalization without synthesizing trades.
- Historical back-adjusted series can leak future corporate-action knowledge.
  The default is `adjustment="raw"`; don't claim past availability of adjusted
  history without a dated adjustment policy.
- IEX coverage is not consolidated SIP coverage; choose and retain the
  selected feed in every experiment. Available subscriptions and historical
  recency differ by Alpaca plan.
- This does not provide order-book data, Alpaca account reconciliation,
  market-session calendars, an AlphaArena-specific feed or authorization to
  execute/paper-trade.
- Live callback errors propagate into SDK handling; automated reconnect,
  gap-fill, backpressure, fault metrics, and durable replay capture are
  separate integration work. Do not dispatch trades directly from callbacks.

## Deterministic validation (no keys, no WebSocket, no trade calls)

```bash
python -m pytest -q tests/test_alpaca_market_data.py
```

CI: `.github/workflows/alpaca-market-data-tests.yml` installs the actual
optional pinned SDK and validates SDK request-model constructors without
contacting Alpaca. The ordinary test suite uses injected client and stream
fakes so it works without the optional dependency.
