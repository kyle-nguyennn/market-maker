# Backtest package — agent invariants

`backtest/` is host-side, **offline-only** research infrastructure.
Preserve the separation from the publishable `alpha/` library.
The existing live `alpha.AlphaPipeline` and `Strategy.decide()`
must be reused, not forked for simulation mode.

1. **No broker or exchange side effects.** Do not import Alpaca trading,
   AlphArena authenticated trade operations, account execution gateways,
   wallet SDKs or exchange order managers. No network or secrets.
2. **Event-time causality is non-negotiable.** For each completed bar, process
   its opening and its declared publication as **separate, globally ordered
   events**. An OPEN that predates a signal publication cannot be filled
   by that signal or canceled by a later signal. Open wins ties over
   publication. Add adversarial tests when changing scheduling.
3. **No feature lookahead.** Only publish normalized completed OHLCV to
   `AlphaPipeline.on_bar` at `PriceBar.available_at_ms`.
   Market price observed at a later opening can mark the portfolio once
   observable, but future bar close/high/low must not enter prior features.
4. **Execution is hypothetical.** Historical bar opening is an optimistic
   deterministic reference fill price, modified by adverse slippage and
   explicit fees. Never claim order-book-accurate execution. Reject maker
   quotes without a separate L2 matching/queue model.
5. **Signed portfolio ledger:** signed quantity, cash, average entry,
   realized/unrealized price P&L, fee and slippage breakdown must reconcile
   to marked equity. Avoid floats in financial values; use fixed Decimal
   context and explicit quantity rounding.
6. **Target exposure is not an order.** The simulator schedules a
   time-gated hypothetical fill from each `TargetExposure`, imposing
   latency, TTL/horizon and bounded target weight. Abstention is not
   liquidation. Repeated equal pending targets must preserve original
   eligibility so that strategies cannot starve their own fills.
7. **Source integrity:** Input bars must be normalized, single-symbol,
   strictly ascending by close time and nondecreasing by first-publication
   time. Do not synthesize missing bars/sessions or silently mix revisions.
   Historical Alpaca publication delay is assumed, not observed.
8. **Document assumptions:** Always state gaps in US equity session
   normalization, splits/dividends, missing minutes, borrow/financing,
   fill probability, book spread and market impact. Report the chosen fee
   and latency policy alongside performance figures.
9. **Run the full CI suite** and add negative tests for any new order state,
   callback or strategy. Maintain `notes/alpha_engine/` ADRs, log and
   handoff at every substantial milestone.
