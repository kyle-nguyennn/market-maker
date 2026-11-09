# Stevens HFTC — SHIFT exchange adapter (initial implementation)

Sources:
- [2027 HFTC rules](https://fsc.stevens.edu/hftc/)
- [Official SHIFT Python client](https://github.com/hanlonlab/shift-python)
- [Trader API](https://github.com/hanlonlab/shift-python/wiki/Trader)
- [Order API](https://github.com/hanlonlab/shift-python/wiki/Order)
- [OrderBookEntry API](https://github.com/hanlonlab/shift-python/wiki/OrderBookEntry)

## Architecture

```text
apps/hftc_shift.py          # CLI, quote loop, conservative execution gating
  HftcQuoter                # Decimal-only top-of-book + inventory-skew strategy
  ShiftVenueClient          # SDK import and all real network calls
  ShiftMarketAdapter        # venue wire representations <-> Decimal internal model

model/market.py             # shared MarketState/BookLevel, sizes in shares
model/order.py              # shared OrderIntent / Quote / ExecutionReport
```

The existing Hyperliquid adapter and its runtime are left intact. SHIFT is **not**
a REST API; its Python wrapper connects using a FIX initiator configuration and a
username/password to a supplied matching environment.

## Install / connect on the SHIFT VM

The public official installer creates a `shift` Conda environment with
Python 3.11 and the compiled SHIFT client. **Do not assume `pip install shift`
installs the supported brokerage client**. On the competition VM, prefer the
organizers' installer and `initiator.cfg`; verify protocol and package version
against what organizers ship. See the official repository README.

```bash
conda activate shift
python -c 'import shift; print("SHIFT SDK available")'
# Install shared strategy/test dependencies into this *same* environment if needed:
python -m pip install -r requirements/common.txt
python -m pytest -q tests/test_shift_adapter.py

# Use provided private FIX initiator config and credentials:
export SHIFT_USERNAME='...'
export SHIFT_PASSWORD='...'
export SHIFT_INITIATOR_CFG='/path/to/initiator.cfg'

# Connect, fetch L2/position/buying power, but do NOT send or cancel any order:
python -m apps.hftc_shift --symbol AAPL
```

The supplied account may trade synthetic symbols instead of AAPL. Confirm via
`get_stock_list()` and select the symbol valid for that round.

**Live trading is disabled by default.** Only after the organizer's test run
validates fees, lot sizes, tick increment, matching rules and session boundaries:

```bash
export SHIFT_ALLOW_TRADING=yes
python -m apps.hftc_shift --symbol AAPL --trade \
  --duration-seconds 300 --flatten-buffer-seconds 30 \
  --order-shares 100 --max-position-shares 1000 \
  --max-order-notional 25000 --tick-size 0.01
```

Unset `SHIFT_ALLOW_TRADING` afterward. Do not commit the FIX configuration,
credentials, session tokens, SSH files, or screen captures. Trading in this
initial runner is a small **test** mode, not a strategy tuned for scoring.

## Venue semantics that MUST not be confused

| Property | SHIFT API | Shared model |
|---|---|---|
| Order and book quantity | integer lots; 1 lot = 100 shares | Decimal shares |
| Portfolio shares | get_shares() returns shares | Decimal shares |
| Prices | SDK float | Decimal; tick-size configured |
| Order ID | UUID string | int or string |
| Limit submission | submit_order() returns no report | must poll get_order() |
| Order cancellation | submit_cancellation(order) | must verify no longer waiting |
| Executions | get_executed_orders(order_id) | count trades, not submissions |

The public SHIFT API does **not** expose post-only, cancel/replace atomicity,
client-provided idempotency keys, or a guaranteed per-request acknowledgment.
The adapter checks that a quote would not cross the most recent known book, but
the market can move after that check. A LIMIT order can therefore be marketable
at the time of arrival, with taker fees. **Never treat this as native post-only.**

Order statuses are asynchronous. `PENDING_NEW` and `PENDING_CANCEL` are not
terminal confirmation. An order submission timeout must halt further entries
until order and portfolio state are reconciled. No blind retry. On failure the
runner attempts to cancel and flatten, then reports unresolved exposure.
For real deployment, add durable journal/restart recovery before raising limits.

## Strategy / scoring considerations

HFTC 2027 is an intraday competition on Dow-30 or synthetic instruments.
The 2026/2027 published format uses weekly points ranked by P&L, a Sharpe
bonus, and **at least 200 executed trades per competition day**. Resting or
cancelled orders are not counted as executions. The initial HftcQuoter quotes
the observed top of book, with a per-lot inventory penalty and hard limits on
position and notional. It deliberately does not force fills to satisfy the
minimum at negative expected value.

What remains before scored deployment:
1. Obtain access and confirm **actual** 2027 competition API package,
   tick increments, market hours, fees/rebates, lot rules, account limits.
2. Verify full round-trip with one 100-share test order, cancel, fill and
   position reconciliation. Test a 1-tick spread and fast book moves.
3. Integrate quote fill/rejection callbacks and persistent order journal.
   The existing Hyperliquid OrderManager assumes native CLOID tracking and
   cannot be plugged into SHIFT unchanged.
4. Add session-aware cutoff from organizer-provided clock/schedule rather
   than inferring or exploiting historical replay dates.
5. Benchmark inventory P&L, fees, passive fill probability, adverse-selection
   markout, cancellation success, and executed fills/day across scenarios.
   Develop regime-aware quoting instead of optimizing fixed constants only.
6. Ensure a tested independent kill switch and a manually executable
   flatten procedure for the competition's actual session.

**Validation status:** Tests are written with a fake SDK and require no
external credentials. No live SHIFT session, broker fill, or end-to-end
simulation has been verified by this change.
