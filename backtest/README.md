# P3 — Causal directional backtesting (offline, no trading)

The `backtest/` package simulates **one US-equity symbol at a time**, using
the exact `alpha.AlphaPipeline`, `SignalModel` and `Strategy.decide()`
interfaces established in merged PR #17. There are no strategy-visible
backtest/live flags, and the feature/signal layer remains a vendored,
independently publishable `alpha/` package.

This research backtester does **not** use Alpaca's trading SDK, submit
AlphArena paper positions, place orders or construct a complete exchange
matching simulator. In particular, it refuses `QuoteProposal` outputs from
market-making strategies: evaluating maker execution requires L2 replay,
queue-position modeling and fill/adverse-selection simulation.

## Event ordering and no-lookahead fill policy

For every normalized completed OHLCV bar:

1. Determine its `bar_open_ms = bar.end_ms - bar.interval_ms`.
2. If a previously submitted target intent has
   `decision_at_ms + latency_ms <= bar_open_ms` **and** is not expired,
   simulate the fill at that bar's **open** plus adverse configured slippage
   and proportional fees.
3. Mark existing cash/position at the bar's **close**, recording a
   timestamped equity point when that bar becomes available.
4. Only now publish the completed bar to `AlphaPipeline.on_bar` at its
   declared `available_at_ms`; this may produce `TargetExposure`.
5. Schedule that target for **future** eligible bar openings. Repeated
   identical pending targets preserve the *original* eligibility/TTL,
   preventing an endlessly reset latency clock. Missing/abstaining signals
   do not implicitly close existing positions or cancel outstanding targets.

**Example:** Bar 10:00–10:01 closes at 10:01:00 but is published at
10:01:01.500. Strategy decides at 10:01:01.500 and latency is 1 second.
The next bar opened at **10:01:00**, before the decision, so its price is
**not** an eligible fill. The 10:02:00 bar open is eligible.

This is *causal* with respect to decision/event timestamps, but the
historical bar-open execution price remains an **idealized fill assumption**:
the engine effectively assumes that a market order ready by that bar
opening could execute at the recorded open, adjusted by static slippage.
There is no spread/impact/order-book simulation and no guarantee such a
fill would have occurred. It is useful for initial conservative timing
experiments, not validated institutional-quality execution research.

Pending intent expiry is the earlier of
`decision_at_ms + order_ttl_ms` or `decision_at_ms + horizon_ms`.
A forecast horizon does **not** automatically flatten a live position:
existing policies abstain rather than close when the signal disappears.
Holding-period/exit rules must be implemented in a separate strategy
policy; do not equate a forecast horizon with the holding period.

## Run from a normalized historical CSV

The reader in `market_handler/historical_bar_reader.py` requires these
exact columns (order arbitrary):

```csv
symbol,end_ms,available_at_ms,open,high,low,close,volume
AAPL,60000,61000,101,101,101,101,1000
AAPL,120000,121000,102,102,102,102,1000
```

Timestamps are integer epoch milliseconds, `end_ms` means **completed**
bar end and `available_at_ms` means when that complete bar could have
first been used. Use genuine captured availability if possible; otherwise
declare the assumption in the data version. The two-line sample above
is merely a schema illustration; at least `lookback_bars + 1` contiguous
real observations are required for the first strategy decision.

```bash
python -m apps.backtest_alpha \
  --csv /path/to/normalized-aapl.csv \
  --source aapl-raw-2026-11-09-lag-assumed-v1 \
  --symbol AAPL \
  --strategy momentum \
  --lookback-bars 20 \
  --target-weight 0.10 \
  --forecast-multiplier 0.25 \
  --fee-bps 1 --slippage-bps 2 \
  --latency-ms 1000 --out-dir /tmp/aapl-momentum-backtest
```

Other `--strategy` choices: `mean_reversion`, `cash` (identical replay
but always abstain: explicit no-trade control). The default model scores,
features, targets, fees and slippage parameters are **illustrative** and
must not be tuned against a reported holdout period.

The command prints JSON metrics, including `net_pnl`, `net_return`,
`max_drawdown`, `total_fees`, `total_slippage_cost`,
`turnover_initial_equity`, `daily_sharpe` (when estimable),
`realized_price_pnl` and `unrealized_price_pnl`. Optional files:

- `equity.csv`: cash, signed position quantity, mark price and portfolio
  equity at each historical bar's publication.
- `fills.csv`: exact source decision ID and signal IDs, decision/eligible/
  actual simulated fill timestamps, signed quantity, fill/open price, fees,
  slippage and realized price P&L.

The simulator uses fixed 34-digit Decimal arithmetic, supports fractional
units via an explicit quantity step, caps absolute target weight, and keeps
a signed average-cost ledger. Positions, cash and unrealized mark-to-market
P&L are updated only through simulated fills, with no synthetic dividends,
interest, borrowing costs or external deposits. A target-weight threshold
suppresses tiny rebalances.

## Run on historical Alpaca bars (requires your own market-data keys)

```bash
pip install -r requirements/alpaca.txt

export ALPACA_API_KEY=...
export ALPACA_SECRET_KEY=...

python -m apps.backtest_alpha \
  --alpaca --symbol AAPL \
  --start-utc 2026-10-01T00:00:00+00:00 \
  --end-utc 2026-10-10T00:00:00+00:00 \
  --source snapshot-2026-11-09 \
  --feed iex --adjustment raw \
  --assumed-publication-lag-ms 1500 \
  --strategy mean_reversion \
  --lookback-bars 20 --out-dir /tmp/aapl-reversion-backtest
```

This sends **only read-only historical market-data requests** when invoked
with user-supplied API credentials. The test suite never calls this API.
The Alpaca source adapter produces the same `PriceBar` as the CSV reader,
and nothing about the strategy/backtest API changes.

**Research limitation:** the Alpaca historical API does not reconstruct
the actual first publication/revision timestamp of every bar. Its
`assumed_publication_lag_ms` is explicitly just a simulation hypothesis.
Default `adjustment="raw"` avoids silently using later adjustment
factors, but unadjusted bars still need corporate actions handled
explicitly to generate trustworthy multi-month return series.
IEX is not consolidated SIP; differences in venue coverage matter.

## Interpreting the metrics

- `net_pnl = final_equity - initial_equity`. The equity curve includes
  adverse fill slippage and charged proportional fees.
- `realized_price_pnl` is the signed average-cost gain/loss realized by
  reductions/closures **before explicit fees**; `unrealized_price_pnl`
  is the marked gain/loss on the remaining position. Slippage is already
  included via the executed fill prices.
- `turnover_initial_equity` is total **absolute executed notional**
  divided by initial equity; it is not an annualized turnover statistic.
- `max_drawdown` uses marked equity at available bar closes and the
  initial equity as its starting peak.
- `daily_sharpe` uses end-of-day marked equity grouped by
  `America/New_York` calendar date, sample standard deviation and
  sqrt(252) annualization. Returns `null` for fewer than two days or
  zero variance. No risk-free rate adjustment; partial sessions,
  dividends, stale bars and irregular observation days may make it
  misleading. Do not interpret it as an audited Sharpe ratio.
- The final outstanding target is **not silently executed at the last
  observed closing price**. If no eligible future opening appears,
  it remains outstanding and unrealized.

## Remaining blockers before trustworthy walk-forward research

1. **US equity market sessions:** `FeatureEngine` currently drops its
   rolling window at every missing contiguous UTC minute. Session
   closes, holidays, halts, corrections and no-trade intervals must be
   handled with a documented, versioned market-calendar policy.
2. **True point-in-time data and revision archive:** historical Alpaca
   availability is assumed; we still need recorded publication
   timestamps, feed revisions and applicable split/dividend knowledge.
3. **Execution realism:** static adverse slippage and next eligible
   bar-open fills are simple deterministic assumptions. Add liquidity
   constraints, bid/ask spreads, delayed orders, partial fills and
   rejection behavior before interpreting performance as tradable.
4. **Walk-forward evaluation:** train/validation/holdout splits, fixed
   parameter choices, stationary comparators and confidence intervals
   are not yet implemented. Compare cash control, momentum,
   mean-reversion and an independently defined buy-and-hold baseline
   on identical eligible historical periods.
5. **Multiple symbols/risk:** this release supports one symbol and one
   strategy per simulator. Never infer net portfolio exposures by simply
   adding conflicting `StrategyEngine` proposals.

Run deterministic tests:
```bash
python -m pytest -q tests/test_alpha_causal_backtest.py tests/test_alpha_backtest_cli.py
```
