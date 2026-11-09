# HFTC 2026 — Competitor Strategy Research and SHIFT Roadmap

**Recorded:** 2026-11-09  
**Scope:** Public HFTC 2026 retrospective; lessons for our SHIFT market-maker on this branch.  
**Status:** Research and proposed experiments, **not** implemented or backtested strategies.

## Primary sources

- [Stevens Hanlon Lab — HFTC 2026 retrospective](https://fsc.stevens.edu/hftc-2026/) — final teams, competition structure, highlighted strategy case studies.
- [Stevens — HFTC 2026 final results (2026-04-29)](https://fsc.stevens.edu/high-frequency-trading-competition-final-results/) — final point totals and round 5 outcome.
- [Stevens — HFTC 2026 week 3](https://fsc.stevens.edu/high-frequency-trading-competition-week-3/) — illiquid round and correlation-agent preview.
- [Stevens — HFTC 2026 week 4](https://fsc.stevens.edu/high-frequency-trading-competition-week-4/) — correlation round, RL-agent next round.
- [Stevens — HFTC 2025 retrospective](https://fsc.stevens.edu/hftc-2025/) — cross-year strategy evidence.
- [Stevens — 2027 HFTC competition rules](https://fsc.stevens.edu/hftc/) — current planning constraints; confirm again with organizer/VM before scored sessions.

**Evidence legend:** **[Published]** = explicitly reported by organizer; **[Inference]** = conclusion suggested by those reports; **[Proposal]** = our own implementation, formula, thresholds, or evaluation procedure. No complete winning-team source code or complete strategy parameters were published in these summaries.

## Executive takeaway

**[Inference]** The most transferable pattern from top competitors is a regime-aware strategy with disciplined execution, not a universally fixed market-making model. We should maintain a single shared execution/risk platform, choose signal/quote policy by market regime, and explicitly track the minimum *executed fills* required for eligibility.

For our bot, the recommended initial target is a **directional, inventory-aware market maker** with:

1. Event-based tick-direction momentum, robust to sparse updates.
2. Inventory-skewed fair value and adaptive quoting/spread/cancellation.
3. Trade-activity eligibility forecast from actual fills, not submissions.
4. Regression trend-following as a separate alpha/strategy-selection policy.
5. Correlation-breakdown and regime-failure checks.

These are *proposals*, not the published composite strategy of any HFTC team.

## 2026 competition and results

**[Published]** Five themed intraday rounds traded on the SHIFT market simulator; weekly P&L rankings under F1-style points, increasing point weights toward later rounds, highest-Sharpe bonus, and a **200-trade-per-day minimum**.

| Round | Theme | Implication for strategy evaluation |
| --- | --- | --- |
| 1 | Fearful / volatility spikes | Test volatility guardrails, sudden spread widening and inventory reduction. |
| 2 | Spikes and Dips | Evaluate continuation versus reversal; markouts after price shocks. |
| 3 | Illiquid | Sparse trades/stale timestamps, limited liquidity; event-based signals. |
| 4 | Correlations | Correlation agents plus zero-intelligence flow; structural-break monitoring. |
| 5 | The Learning Curve | Reinforcement-learning-driven agents; adverse selection and faster policy response. |

Final podium **[Published]**:

| Rank | Team | University | Final score |
| --- | --- | --- | --- |
| 1 | The Bagholders | Rutgers | 560 |
| 2 | Stevens Quants | Stevens Institute of Technology | 460 |
| 3 | PumpnDump | Stony Brook | Not included in the cited final-results text |

The retrospective highlights an additional team, Columbia-traders, for a particularly relevant market-making signal. Being featured is **not** evidence they finished on the podium.

## Case study A — The Bagholders: abandon an invalidated relative-value model

**[Published]** In the Round 4 correlation market, the team investigated a three-stock approximate relation:

$$
P_1 \approx 100 + \beta (P_2-P_3), \qquad \beta \approx 1.25.
$$

Conventional statistical arbitrage was unattractive because **one-lot execution limits** made precise hedge ratios difficult and the legs **diverged late in the session**. The team instead built a **trend-following algorithm** scoring instruments on **20- and 30-minute linear regressions**, rewarding stronger R-squared and penalizing maximum drawdown. They rotated toward the strongest trend, exploiting correlation breakdown in the last ~90 minutes.

**[Inference]** A large apparent residual is not automatically a mean-reversion opportunity. When the structural relationship breaks, adding to the trade can be precisely the wrong response.

**[Proposal]** Build a separate trend/regime signal service:

- Maintain time-window returns/prices for each instrument, track regressions over 20- and 30-minute windows.
- Compute fitted slope, goodness-of-fit, volatility-adjusted slope, rolling drawdown and stability.
- Rank instruments by a *candidate* score, **not purported to be the winner's formula**:

$$
S_i = w_1 \frac{|\widehat{\beta}_i|}{\widehat{\sigma}_i + \varepsilon}
+ w_2 R_i^2 - w_3 DD_i.
$$

- If introducing cross-asset relative value, monitor spread residual stationarity, hedge-ratio stability, rolling correlations, and practical lot-size hedge error.
- Halt relative-value entries when assumptions fail; do not presume dislocations must converge.
- Backtest different windows and feature weights; the published team coefficients are unknown.

## Case study B — Columbia-traders: direction-only market making

**[Published]** In illiquid small caps **SHOO, CROX, PZZA**, the team always quoted **both sides**, but biased placement using the most recent **15 signed price-change directions**:

| Condition | Regime |
| --- | --- |
| At least 10 upward changes among last 15 | BULL |
| At least 10 downward changes among last 15 | BEAR |
| Neither condition | NEUTRAL |

Inventory was capped at **±5 lots**. The published motivation was robustness when timestamps stagnated and return magnitudes were tiny, making some conventional time-bar/return signals unreliable.

**[Important unknowns]** The retrospective does **not** fully specify: treatment of zero-price-change ticks; whether signal uses last trades or quote changes; exact quote offsets/size schedule; fill-risk controls; cancellation cadence; fees; session P&L; or profitability attributable solely to this signal.

**[Proposal]** Implement a venue-neutral tick-direction feature:

- Consume deduplicated ordered **last-trade price updates**; define zero-change policy explicitly and expose it as configuration.
- Keep a rolling window of 15 *valid* signs and classify with the published 10-up / 10-down thresholds.
- Require a complete warm-up window; report NEUTRAL or abstain before enough ticks.
- Maintain separate size/time gap and price-jump alerts. Sign-only features discard magnitude, so a single large gap cannot count the same as a safe microtick from the risk controller's perspective.
- Use momentum to shift reservation price, while inventory independently dampens accumulation.
- Compare this rule against symmetric baseline, time-series returns, order-book imbalance, and a randomized-sign null model.

A **proposed** reservation-price model (not disclosed team code):

$$
r_t = m_t + \alpha_t - \gamma q_t \sigma_t^2 \tau,
$$

where midprice is m, alpha is our signal adjustment, q is inventory, sigma volatility and tau a chosen horizon. Quoting bid/ask offsets around r then depends on expected fill, adverse selection, fees and inventory. Never confuse this model with a verified strategy from Columbia-traders.

## Case study C — Stevens Quants: P&L can be disqualified

**[Published]** Their strategy “The Oracle” identified a historical-replay information leak through the session date returned by SHIFT's last-trade-time API. After precomputing largest movers for **5,595 historical dates**, they traded long/short positions and reported about **$16,538 realized P&L** and **0.96 Sharpe** for the highlighted session. They did **not** meet the **200-fill daily minimum**, and that round's result was disqualified. The organizers changed competition rules/back-end behavior in response.

**Do not replicate the historical replay exploit.** It is no longer a supported or appropriate strategy target.

**[Inference]** Submitting hundreds of orders is not equivalent to executing 200 qualifying trades. Passive quotes can remain unfilled during one-way markets.

**[Proposal]** Implement a dedicated *activity and execution controller*:

- Count only organizer-qualifying execution events. Reconcile execution events across callbacks and historical order fills; deduplicate by stable execution ID where available.
- Track required fill rate, fill-rate estimate, remaining session time, and eligibility risk. A simple diagnostic is:

$$
\lambda_{\mathrm{required}} =
\frac{\max(0,200-N_{\mathrm{qualifying\ fills}})}
{T_{\mathrm{remaining}}}.
$$

- Distinguish **trade/transaction counts** from partial fills, shares, order count, and cancellation count. Organizer-specific qualifying definitions must be verified.
- Adjust quoting intensity/select symbols based on estimated *net* value, inventory, fees, and eligibility—not unconditional marketable trades.
- Persist execution ledger and recover state on restart. Raise alarms when projected daily qualifying fills fall short.
- Test sessions with directional price movement and nearly zero passive fills. Fail clearly and report eligibility risk rather than hiding the shortfall.

## Other podium performer — PumpnDump

**[Published]** PumpnDump finished third overall; the organizer reported strong performance in the “Spikes and Dips” regime and high Sharpe performance earlier in the competition.

**Unknown:** The retrospective does **not** publish sufficient implementation detail to attribute its success to a particular momentum, reversion, breakout or market-making formula.

**[Proposal]** Test a market-shock classifier and compare four baselines:
1. Always-on symmetric top-of-book quotes.
2. Volatility-widened quotes.
3. Directionally shifted quotes.
4. Brief quote withdrawal / inventory reduction after exceptional jumps.

Use actual markout, fill opportunity cost, net P&L and execution-minimum attainment to assess the policies.

## Cross-check: HFTC 2025 (supporting, not 2026 results)

**[Published]** The 2025 retrospective reports:

- **ClockSense Trading (1st)** switched strategies across themes, including momentum-weighted selection, mean reversion and market-making discipline.
- **Spread Snipers (2nd)** evolved a market-making core with trend filters, VWAP/MACD/Bollinger diagnostics, inventory- and order-book-imbalance-aware reservation prices, stale-order cancellation and improved response speed in the RL round. Naive best-bid/ask quoting struggled in trends.
- **Money Never Sleeps (3rd)** used an inventory-conscious Avellaneda–Stoikov style.

**[Inference]** The consistent lesson across 2025–26 is regime dependence, execution control, and disciplined risk—not a demonstrated need for opaque deep learning.

## Changes we should make on this branch (planned only)

Current integration:

- **exchanges/shift/client.py** — thin SHIFT SDK client.
- **market_handler/shift_adapter.py** — wire/book/order to shared Decimal model; 100 shares per SHIFT lot.
- **apps/hftc_shift.py** — guarded, read-only-by-default single-symbol demonstration quoter.
- **docs/HFTC_SHIFT.md** — setup, API/venue caveats, current validation status.
- **tests/test_shift_adapter.py** — offline contract tests (not reported as executed in the initial integration).

Recommended architecture (venue-neutral signal/policy; exchange-specific transport only):

~~~text
SHIFT SDK / book / last trade / execution callbacks
    -> normalized event and execution stream
    -> feature engine (tick signs, 20/30-min regressions, L2, markouts)
    -> regime model / selector
       -> neutral MM | directional MM | trend-following | shock/defensive
    -> target fair value and target inventory
    -> fill-aware quote optimizer
    -> hard risk + competition eligibility gates
    -> durable SHIFT order lifecycle -> SHIFT SDK
~~~

### Priority backlog and acceptance criteria

| Priority | Work item | Minimum acceptance criteria |
| --- | --- | --- |
| P0 | Fill ledger and daily qualifying count | Distinguish fills from orders; no double count on callback/reconnect; report progress to 200. |
| P0 | Tick-direction signal | Published 15/10 rule reproduced on synthetic event sequence; warm-up/zero-tick policies explicit. |
| P0 | Inventory-aware reservation quoting | Deterministic long/short skew tests; remain inside configured position, order and notional limits. |
| P0 | Execution/recovery hardening | Cancel/ack/reconciliation tests; never retry an ambiguous order blindly. |
| P1 | Adaptive spread, size and cancellation | Benchmark vs fixed top-of-book under quiet, trending, illiquid and shock scenarios, after fees. |
| P1 | 20/30-minute trend scoring | Synthetic-trend unit tests; score and risk metrics recorded; no position amplification on structural-break failure. |
| P1 | Multi-symbol budget allocator | Concentration and available buying-power limits; per-symbol and aggregate exposure visible. |
| P1 | Fill-probability / markout instrumentation | Observe fill rate, expected edge, realized adverse-selection markout, fees and quote lifetime. |
| P2 | Cross-asset correlation health check | Detect induced correlation breakdown and shut down invalid pairs trades. |
| P2 | Adversarial/RL-agent scenario testing | Quantify quote staleness, latency sensitivity and realized slippage. |

### Evaluation rubric

All experiments should report **after-fee realized/unrealized P&L, Sharpe, max drawdown, max gross/net exposure, number of organizer-qualifying fills, qualifying-session rate, post-fill 1/5/30-second markout, order reject/cancel rate and quote lifetime**. Record both the competitor ranking metric and failure/disqualification conditions.

Ablation plan:

1. Baseline SHIFT top-of-book quoter (presently demonstration only).
2. + inventory reservation-price penalty.
3. + 15-tick direction signal.
4. + adaptive spread/cancellation and jump guards.
5. + 20/30-min trend / instrument allocation.
6. + eligibility-aware activity forecasts.

Replay multiple independent market regimes and random seeds. Optimize **competition scoring**, not raw P&L in isolation. Keep a holdout scenario for validation and avoid overfitting one round.

### Live-market safety and known limitations

- This note does **not** change runnable strategy code.
- Confirm actual competition **fees, trading hours, market symbols, lot sizes, qualifying execution semantics, and SDK callbacks** before scored deployment.
- The SDK does not promise native post-only or atomic replacement; cancel-then-reprice is exposed to crossing and stale state. Never assume maker rebates.
- Quotes/orders/fills must reconcile after connection loss; maintain manual/automated kill-switch and flatten procedure.
- SDK sizes are in 100-share lots, while our common model uses Decimal shares.
- The current guarded demo lacks tested session recovery, fill forecast, dynamic regime models and broad multi-symbol execution.
- Agenthon simulation experience can guide stress tests, but we cannot assume SHIFT uses identical agent dynamics or fill rules.

## Next implementation milestone

**Implement event-based 15-tick momentum + an exact fill/activity ledger first**, with fake-SDK unit tests and a fully reproducible offline scenario harness. Then evaluate directional/inventory-skewed quotes relative to the baseline. Follow with the regression regime selector and multi-symbol allocation.

This order prioritizes demonstrable robustness and competition eligibility before model complexity.
