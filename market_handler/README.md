# Market data adapters — host application boundary

`market_handler/` owns host-specific **market-data acquisition and
normalization**, not alpha features, forecasts, or trading-policy decisions.

## Separate source adapters, shared observation contracts

- `hyperliquid_market_data_handler.py`: current Hyperliquid REST/WebSocket
  feed adapter, maintains one instrument's latest validated **L2 book**
  `model.market.MarketState`. This is **not** an OHLCV bar source and is not
  an AlphArena API client.
- `alpaca_stock_market_data.py`: optional official Alpaca stock-data SDK
  integration; `AlpacaHistoricalBarProvider` retrieves IEX/SIP historical
  minute bars with an explicit publication-lag assumption;
  `AlpacaMinuteBarStream` subscribes to live completed minute bars and
  records local UTC callback receipt time. Both normalize to the same
  `alpha.observations.PriceBar`; neither uses an Alpaca trading API.
  See [ALPACA.md](./ALPACA.md) for installation, examples, and limitations.
- `historical_bar_reader.py`: strict file-based reader that returns
  `alpha.observations.PriceBar`. It handles CSV parsing and exact Decimal
  conversion; it does not perform feature calculation or infer calendar gaps.
- Future host adapters (additional market-vendor historical/live equity feeds, external
  bar feeds, Parquet, tick-to-bar aggregation) should normalize into the
  **same** `alpha.observations.PriceBar` when the source is OHLCV.

The `alpha/` package owns the *normalized observation contract* but **never
imports this directory**. This means it can be extracted into its own Python
distribution later without an exchange SDK, file reader or Redis dependency.

## Replay and live

```text
historical CSV -> historical_bar_reader ─┐
                                        ├─> PriceBar -> FeatureEngine.on_bar
live feed -> source-specific normalizer ─┘
                                                  -> SignalEngine -> Strategy
```

Use the observation's actual `available_at_ms` rather than treating the
completed bar's `end_ms` as an implicit publication time. Ensure that data
source/version, adjusted/unadjusted price semantics, trading sessions and
publication delays are documented by the adapter. AlpArena US equities will
require session-aware normalization rather than assuming bars are continuous
through market closures.

**AlphArena distinction:** `exchanges/alpharena/client.py` is for submitting
and reconciling paper positions. The published AlphArena `/api/v1` docs do
not list historical OHLCV or a live-price stream. Do not fabricate an
`AlphArenaMarketDataHandler`. Select a real equities market-data provider
and implement its adapter here, or in a dedicated host `market_data/`
source package if the number of adapters grows.

**Not implemented here:** a generic multi-vendor live bar aggregator, trading-calendar adjustment, reliable historical tick capture,
Redis history writer, or any trading-dispatch integration. Keep the hot path
independent of Redis and preserve existing L2 handler's safety gates.
