# AGENTS.md — Kyle's Market-Making Bot

## Mission

This repository builds a robust market-making bot for two related purposes:

1. Use Optiver Ready Trader Go (RTG) as a practical training environment.
2. Reuse the strategy interfaces, simulator, risk controls, calibration workflow, and observability as scaffolding for the Akuna 2026 trading challenge once its specifications are released.

The project is also interview preparation. Favor code and experiments Kyle can explain clearly: assumptions, equations, units, trade-offs, failure modes, and evidence matter as much as PnL.

RTG and Akuna are separate venues behind adapters. Never embed competition-specific messages or rules in core strategy logic.

## Goals

- Build a deterministic, fixed-parameter, two-sided quoting baseline.
- Add explicit inventory management based on Avellaneda–Stoikov concepts.
- Estimate fill intensity from data, including `k`, rather than treating `k` as a live quote-width knob.
- Preserve a fixed-`k` control strategy before attempting adaptive estimation.
- Run reproducible simulator and replay experiments across regimes and seeds.
- Attribute PnL and diagnose spread capture, inventory risk, costs, and adverse selection.
- Enforce hard risk limits independently of the strategy.
- Keep the architecture portable to the Akuna 2026 API and rules.

## Non-goals

- Do not optimize for one replay, seed, or known scenario.
- Do not claim textbook Avellaneda–Stoikov assumptions perfectly describe a real exchange.
- Do not introduce online adaptation before the fixed baseline is stable and measured.
- Do not hand-tune `k` to widen or tighten quotes.
- Do not weaken risk controls to improve backtest PnL.
- Do not mix exchange I/O, strategy logic, calibration, accounting, and reporting in one component.
- Do not optimize latency before correctness, safety, observability, and profiling.

## Core conventions

- State units explicitly. Distinguish ticks from price units, seconds from nanoseconds, and quantity from lots.
- Use integer ticks at exchange boundaries. Convert model prices in one tested boundary function.
- Use monotonic exchange or simulator time for event ordering and durations.
- Every experiment requires a versioned configuration, code revision, dataset/scenario identifier, and random seed.
- Prefer immutable event records, typed state, narrow interfaces, and pure strategy calculations.
- Centralize validated parameters; avoid magic numbers.
- Reject stale or invalid input explicitly.
- Use structured logs for analysis and concise operational logs for humans.
- Preserve simple baselines when adding sophisticated variants.

## Architecture

### Exchange adapters

- Translate RTG or Akuna messages into normalized market, order, fill, error, and lifecycle events.
- Translate validated order actions into venue requests.
- Own protocol details, sequencing, throttling, connectivity, and venue constraints.
- Contain no fair-value, reservation-price, or spread logic.

### Market state

- Maintain book state, best bid/ask, mid, spread, optional microprice, timestamps, and quality flags.
- Detect stale data, missing sides, crossed books, sequence gaps, and abnormal jumps.
- Expose normalized snapshots rather than venue-specific messages.

### Strategy engine

- Consume market state, inventory, live orders, fills, and configuration.
- Produce fair value, reservation price, target spread, inventory skew, sizes, and quote intents.
- Never send or cancel orders directly.

### Order manager

- Reconcile desired quotes with live and pending orders.
- Apply tick rounding, duplicate suppression, price/size bounds, and cancel/replace policy.
- Track client/exchange IDs, acknowledgements, partial fills, cancels, rejects, and pending actions.
- Be idempotent under duplicate callbacks.

### Risk manager

- Remain independent of the strategy and able to veto or reduce intents.
- Enforce inventory, size, notional, loss, message-rate, stale-data, and outstanding-order limits.
- Own the kill switch: cancel risk and prevent new orders.

### Portfolio/accounting

- Track position, cash, fees/rebates, realized PnL, unrealized PnL, and mark-to-market PnL.
- Reconcile fills exactly using a documented mark and sign convention.

### Calibration/research

- Produce versioned volatility, fill-intensity, latency, and adverse-selection estimates.
- Stay off the hot path unless an explicitly tested online estimator is enabled.

### Simulator/replay

- Drive the same domain interfaces used by live competition adapters.
- Model asynchronous acknowledgements, latency, cancels, partial fills, fees, message limits, and fill/queue assumptions.
- Label synthetic assumptions clearly; simulated fills are not ground truth.

### Observability/reporting

- Record decisions and outcomes for replay, debugging, calibration, and experiment comparison.
- Produce summaries without changing strategy behavior.

Dependencies point inward: adapters and simulators depend on core domain interfaces; core strategy never depends on RTG or Akuna protocol classes.

## Required strategy state

- Event and market-data timestamps.
- Best bid/ask, sizes, mid, spread, and optional microprice.
- Signed inventory `q`, cash, fees, and marked PnL.
- Live/pending orders with side, price, remaining size, age, and queue information if known.
- Recent fills and signed order flow over configured windows.
- Volatility `sigma` plus estimator metadata.
- Fill-intensity estimates `A` and `k`, calibration window, units, sample size, and version.
- Risk aversion `gamma`, effective horizon `tau`, and quote constraints.
- Connection, staleness, throttling, and kill-switch status.

Recommended event order:

1. Validate and normalize the event.
2. Update market, order, and portfolio state.
3. Recompute hard risk status.
4. Ask the strategy for quote intents if quoting is allowed.
5. Apply risk filters and constraints.
6. Reconcile intents with live orders.
7. Send actions and log the decision and outcomes.

Process fills and order updates even when quoting is disabled.

## Model notation and equations

Use these symbols consistently in code, docs, plots, and tests:

- `s`: reference price, normally mid or a documented fair-value estimate.
- `q`: signed inventory; positive is long.
- `sigma`: volatility in price units per square-root time.
- `gamma`: inventory risk-aversion coefficient.
- `tau = T - t`: remaining or effective horizon.
- `delta`: non-negative distance from the reference price.
- `A`: fill-intensity scale.
- `k`: exponential intensity decay, in reciprocal price units.

The Avellaneda–Stoikov reservation price is the conceptual baseline:

```text
r = s - q * gamma * sigma^2 * tau
```

A long position lowers `r`, encouraging sales and discouraging additional buys. A short position does the reverse.

Use the empirical arrival model:

```text
lambda(delta) = A * exp(-k * delta)
```

For the canonical exponential-arrival model, a common symmetric total spread is:

```text
spread_star = gamma * sigma^2 * tau
              + (2 / gamma) * log(1 + gamma / k)
half_spread = spread_star / 2
bid_star = r - half_spread
ask_star = r + half_spread
```

If the implementation uses an infinite-horizon approximation, asymmetric intensities, microprice, alpha, discrete inventory states, fees, latency buffers, or a heuristic spread, document the exact formula. Do not label a heuristic as canonical Avellaneda–Stoikov.

Final quotes must also satisfy:

- Side-aware tick rounding.
- `bid < ask` unless venue rules explicitly say otherwise.
- No accidental marketable orders.
- Inventory, order-size, and notional limits.
- Price collars, staleness rules, and message-rate limits.
- A spread floor reflecting fees, expected adverse selection, and latency cost where applicable.

Resolve crossed or collapsed rounded quotes deterministically and log the binding constraint.

## Baseline strategy skeleton

```text
on_event(event):
    normalized = adapter.normalize(event)
    state.apply(normalized)
    portfolio.reconcile(normalized)
    orders.reconcile_exchange_update(normalized)

    risk_status = risk.evaluate(state, portfolio, orders)
    if not risk_status.may_quote:
        orders.cancel_as_required(risk_status.reason)
        log_and_return()

    market = market_state.snapshot()
    if not market.is_valid_and_fresh:
        orders.cancel_all("invalid_or_stale_market")
        log_and_return()

    fair = fair_value_model.compute(market, state)
    sigma = volatility_estimator.value()
    tau = horizon_policy.effective_horizon(now)
    reservation = fair - inventory * gamma * sigma^2 * tau

    raw_spread = quote_model.spread(gamma, sigma, tau, fixed_k)
    spread = constraints.apply_cost_and_market_floors(raw_spread, market)
    bid = round_bid(reservation - spread / 2)
    ask = round_ask(reservation + spread / 2)
    sizes = sizing_policy(inventory, limits, market)

    desired = build_quote_intents(bid, ask, sizes)
    safe = risk.filter(desired, state, portfolio)
    actions = orders.reconcile(safe)
    adapter.send(actions)
    log_decision(market, state, model_inputs, desired, safe, actions)
```

## Fixed-`k` baseline

Build and keep this control before adaptive control:

- Estimate one fixed `k` from a broad training sample.
- Estimate fixed `A` when absolute intensity is needed.
- Use a documented volatility estimator and fixed update cadence.
- Hold execution and risk parameters constant within each run.
- Use deterministic inventory skew, sizing, and order reconciliation.
- Validate across multiple days/regimes, scenarios, and random seeds.

Never delete or silently mutate this baseline when adding an adaptive variant.

## Calibrating fill intensity

`k` is estimated from quote exposure and fills; it is not a live aggressiveness knob.

For each quote opportunity or resting interval, record:

- Instrument, side, start/end time, quote price, reference price, and `delta` in price units and ticks.
- Eligible exposure duration.
- Size, queue-ahead estimate if known, age, amendments, and cancellation reason.
- Fill indicator, quantity, time to first fill, and partial-fill details.
- Spread, volatility, imbalance, trade rate, time bucket, and other regime features.
- Latency and data-quality flags.

Include non-fills and time at risk. Treat cancels and end-of-window observations as censored where appropriate. Avoid inferring a fill opportunity merely because a later trade printed at the price.

For distance buckets `delta_j`, a basic exposure estimator is:

```text
lambda_hat(delta_j) = fills_j / eligible_exposure_time_j
log(lambda_hat(delta_j)) = log(A) - k * delta_j
```

Prefer a Poisson likelihood or survival/hazard model using individual exposures when data permits. Report uncertainty, sample size, fit diagnostics, residuals, and out-of-sample stability. Account for side, regime, queue position, and censoring when material.

Use chronological train/validation/test splits. Store calibration artifacts containing data range, feature definitions, units, estimator version, `A`, `k`, uncertainty, sample counts, code revision, and configuration hash.

Interpretation:

- Larger `k` means fill intensity decays faster with distance.
- `A` controls the overall intensity scale.
- Queue position, volatility, latency, spread regime, and trade flow can change fills without representing a true change in `k`.
- If the exponential model fits poorly, preserve that result and compare alternatives against the fixed baseline.

## Adaptive-control cautions

- Do not refit `k` continuously from a tiny or endogenous sample.
- The strategy changes which observations it collects; account for selection effects.
- Separate estimator cadence from quote cadence.
- Require minimum effective samples, smooth estimates, bound changes, and define fallbacks.
- Version and log each parameter update and its reason.
- Test feedback loops: fills change estimates, estimates tighten quotes, inventory grows, and estimates react again.
- Gate adaptive variants behind configuration flags.
- Compare them against the unchanged fixed baseline with walk-forward tests.
- Test regime shifts, sparse data, parameter shocks, and estimator failure.
- Hard risk limits never adapt away.

## Risk and safety

Put limits in validated configuration with conservative defaults:

- Hard maximum absolute inventory.
- Soft inventory threshold that increases skew or reduces same-direction size.
- Maximum order size and total outstanding quantity.
- Maximum notional exposure where relevant.
- Session loss and drawdown limits.
- Order/cancel rate limits with safety headroom.
- Maximum market-data age and sequence-gap policy.
- Price collars and maximum reference-price distance.
- Maximum acknowledgement/connection delay.
- Kill switch and orderly shutdown behavior.

On a hard breach, stop adding risk and cancel affected orders. Flattening must be an explicit venue-aware policy; uncontrolled emergency market orders can worsen losses. Log every veto, clamp, and kill transition with the observed value and limit.

## Simulator and venue integration

- Simulator, RTG, and future Akuna adapters emit the same domain events and consume the same order intents.
- Model acknowledgements and fills asynchronously.
- Make latency configurable by message type.
- Identify the fill model in every report: touch, probabilistic, queue-based, or replay matching.
- Support deterministic seeded runs and event-by-event replay.
- Inject rejects, delayed acknowledgements, partial fills, post-cancel fills, dropped/stale data, disconnects, and rate-limit pressure.
- Keep venue rules in adapters/configuration.
- When Akuna 2026 rules arrive, create a compatibility-gap analysis before changing core interfaces.
- Recalibrate for Akuna-relevant conditions; never carry RTG parameters across blindly.

Increase simulator realism incrementally. A complex unvalidated simulator is not automatically more trustworthy than a simple transparent one.

## Experiment protocol

Every strategy claim must come from a named, reproducible experiment:

1. State the hypothesis before running.
2. Identify the unchanged baseline and primary change.
3. Freeze code revision, configuration, calibration artifact, data/scenario range, and seeds.
4. Use time-separated train, validation, and final test data.
5. Run multiple seeds and relevant regimes; retain unfavorable runs.
6. Record performance, execution, calibration, and risk diagnostics.
7. Report aggregate results, dispersion, and worst cases.
8. Diagnose discrepancies through replay and PnL attribution.
9. Keep negative results and rejected hypotheses.

Prefer one-factor comparisons first. Define parameter-search spaces in advance and preserve all results. Never tune on the final holdout.

## Metrics

### Performance and risk

- Gross and net PnL.
- Realized, unrealized, and marked PnL.
- PnL per time, fill, and traded volume.
- Fees and rebates.
- Maximum drawdown and terminal inventory value.
- Distribution across regimes, scenarios, and seeds.
- Inventory mean, variance, extrema, time near limits, and recovery after shocks.

### PnL attribution

At minimum separate:

- Spread capture relative to the chosen contemporaneous mark.
- Inventory mark-to-market from later reference-price movement.
- Fees/rebates.
- Alpha/fair-value contribution if a predictive reference is used.
- Reconciliation residual, which should be near zero.

Document the identity and sign convention. Do not double count spread capture and inventory revaluation.

### Execution

- Fill count, volume, rate, and intensity by side and distance.
- Eligible exposure and time to fill.
- Quote uptime and two-sided uptime.
- Distance to reference and quoted-spread distribution.
- Cancel/replace rate, order lifetime, rejects, and throttling.
- Partial and post-cancel fills.
- Signed markouts and realized spread at multiple horizons.
- Participation and buy/sell imbalance.
- Market-to-decision, decision-to-send, send-to-ack, and send-to-fill latency percentiles.

### Model quality

- `A`, `k`, uncertainty, sample size, and calibration age.
- Observed versus predicted intensity by distance.
- Forecast versus realized volatility.
- Adaptive parameter update frequency and magnitude.
- Frequency of constraints overriding raw model quotes.

## Structured logging schema

Use timestamped records with stable event names and `schema_version`. A decision record must explain why the bot acted.

Common fields:

```text
schema_version, run_id, strategy_id, config_hash, code_revision,
event_id, event_type, exchange_ts, receive_ts, decision_ts, send_ts,
instrument, side, venue_sequence, random_seed
```

Market fields:

```text
best_bid, best_ask, bid_size, ask_size, mid, microprice, spread_ticks,
book_age, volatility, imbalance, market_state_valid, invalid_reason
```

Strategy fields:

```text
fair_price, inventory, gamma, sigma, tau, A, k, parameter_version,
reservation_price, raw_half_spread, constrained_half_spread,
target_bid, target_ask, target_bid_size, target_ask_size,
binding_constraints, decision_reason
```

Order/fill fields:

```text
client_order_id, exchange_order_id, order_state, order_price,
order_size, remaining_size, order_age, action, action_reason,
fill_price, fill_size, liquidity_flag, fee, ack_latency, fill_latency
```

Risk/accounting fields:

```text
cash, position, gross_pnl, net_pnl, realized_pnl, unrealized_pnl,
drawdown, risk_state, risk_limit_name, risk_observed_value,
risk_limit_value, kill_switch_state
```

Never log secrets. Version schema changes and update readers/tests.

## Test requirements

### Unit tests

- Reservation-price sign and magnitude for long, flat, and short inventory.
- Spread formula, units, and edge cases.
- Side-aware tick rounding and non-crossing enforcement.
- Inventory-aware sizing and hard limits.
- Stale, crossed, or missing book behavior.
- PnL accounting and attribution from known fill sequences.
- Order transitions, duplicates, partial fills, rejects, cancels, and post-cancel fills.
- Configuration parsing, units, bounds, and invalid values.
- Calibration on synthetic data with known `A` and `k`.

### Integration tests

- Full market-data-to-order lifecycle through adapter boundaries.
- Fill propagation to cash, inventory, risk, strategy, and logs.
- Disconnect/reconnect and sequence-gap recovery.
- Delays and throttling do not create duplicate live orders.
- Kill switch cancels orders and prevents new exposure.
- Simulator and venue adapters pass common contract tests.

### Property/simulation tests

- Identical input, configuration, and seed produce identical results.
- Inventory and outstanding size never breach hard limits under randomized events.
- Prices always use valid ticks and quotes maintain required ordering.
- Cash, position, fees, and PnL reconcile to fills.
- Safety holds under volatility shocks, sparse books, burst fills, latency, rejects, and stale data.
- Fixed-`k` golden-run diagnostics remain within documented tolerances.

Do not assert profitable PnL on a single random path. Test invariants exactly and statistical behavior with justified sample sizes and tolerances.

## Performance expectations

- Correctness and bounded risk precede micro-optimization.
- Keep the event hot path non-blocking: no plotting, full-history scans, file reads, or remote calibration calls.
- Use incremental estimators and bounded live data structures.
- Profile before optimizing allocation or copying.
- Measure latency percentiles, not only averages.
- Establish an environment-specific latency budget once competition limits are known.
- Logging must not stall quoting; use buffered/asynchronous output with a safe failure policy.
- Optimizations must preserve determinism and pass all risk/accounting tests.

## Coding standards

- Follow the repository's formatter, linter, type checker, build system, and naming conventions.
- If the repo is new, add the smallest conventional toolchain and document commands.
- Prefer explicit domain names over unexplained abbreviations.
- Comment assumptions, units, invariants, and venue quirks—not obvious syntax.
- Keep public interfaces narrow and typed.
- Prefer composition and pure functions over deep inheritance.
- Preserve error context; avoid broad silent catches.
- Never compare floating-point prices for exact equality. Prefer ticks at exchange boundaries.
- Keep notebooks/research scripts separate from production modules. Move validated logic into tested libraries.
- Never commit credentials, private challenge data, generated logs, large datasets, or build artifacts.

## Milestones

### 0. Repository and contracts

- Establish build, test, lint, type-check, configuration, and run commands.
- Define normalized events, snapshots, intents, fills, and adapter interfaces.
- Add run manifests, structured logs, and deterministic seed handling.

### 1. Safe naive baseline

- Integrate RTG market data and order lifecycle.
- Quote fixed symmetric prices around mid/fair value.
- Add order reconciliation, accounting, hard limits, stale-data behavior, and kill switch.
- Verify with deterministic simulator/replay tests.

### 2. Inventory-aware baseline

- Add reservation-price skew and inventory-aware sizing.
- Add volatility estimation and cost/spread floors.
- Add PnL attribution, markouts, execution metrics, and run reports.

### 3. Fixed fill-intensity model

- Collect quote exposure, fills, and censoring data.
- Fit and validate versioned fixed `A`/`k` artifacts.
- Add the fixed-`k` Avellaneda–Stoikov spread as a selectable variant.
- Compare out of sample with naive and inventory-aware controls.

### 4. Robust experimentation

- Add latency, queue/fill, reject, partial-fill, disconnect, and stale-data scenarios.
- Run sensitivity and walk-forward tests across regimes and seeds.
- Profile latency and strengthen operational failure handling.

### 5. Gated adaptation

- Prototype adaptive volatility or intensity one at a time.
- Add smoothing, sample thresholds, bounds, fallback, versioned logs, and feedback-loop tests.
- Promote only robust out-of-sample improvements versus the unchanged fixed baseline.

### 6. Akuna 2026 adapter

- Record released APIs, products, scoring, limits, fees, and data semantics.
- Write a compatibility-gap analysis.
- Implement the adapter and competition configuration without contaminating core logic.
- Recalibrate using permitted, relevant data.

## Interview-preparation outcomes

The repository and notes should let Kyle explain:

- Why market makers earn spread and lose it through adverse selection and inventory risk.
- How reservation price converts inventory risk into quote skew.
- Avellaneda–Stoikov assumptions and where exchange mechanics violate them.
- The meaning and units of `A`, `k`, `gamma`, `sigma`, and `tau`.
- Estimating `k` from exposure and fills, including censoring and selection bias.
- Why fixed parameters are the control before online adaptation.
- How tick size, queue position, fees, latency, volatility, and rate limits affect quotes.
- How partial fills, order races, stale data, and disconnects create risk.
- How PnL is reconciled and how markouts reveal adverse selection.
- How to distinguish genuine improvement from overfitting and simulator artifacts.

Experiment notes should state: question, hypothesis, assumptions, method, configuration, results, failure modes, and next step.

## Instructions for Codex

When changing this repository:

1. Read this file and any more specific nested `AGENTS.md` first.
2. Inspect architecture, commands, configuration, tests, and working-tree changes. Preserve unrelated user work.
3. State model assumptions and units when touching strategy math.
4. Make the smallest coherent change that advances the requested milestone.
5. Preserve adapter/core separation and the fixed baseline.
6. Add tests for affected formulas, invariants, state transitions, risk, accounting, and schemas.
7. Run narrow checks first, then broader test/lint/type-check suites when practical.
8. Provide a reproducible experiment configuration for strategy changes.
9. Never claim performance improvement without a baseline comparison and evidence.
10. If rules, data, units, or simulator behavior are ambiguous, inspect available sources and state conservative assumptions.
11. Never weaken hard limits to improve PnL or pass a test.
12. Never hand-tune `k` as a live spread control; require a calibration artifact or labeled sensitivity experiment.
13. Version configuration and log-schema changes and preserve compatibility where reasonable.
14. Report what changed, why, validation performed, assumptions, and follow-up risks.

For reviews, prioritize: uncontrolled risk or wrong order state; incorrect accounting; look-ahead/data leakage; unit/sign errors; non-reproducibility; model mismatch; measured performance; style.

## Definition of done

A change is complete only when:

- Behavior, assumptions, and units are clear.
- Relevant risk invariants hold.
- Tests cover normal, boundary, and failure paths.
- Logs make decisions and outcomes diagnosable.
- Configuration and defaults are documented.
- Reproduction settings exist where applicable.
- The fixed baseline still works unless removal was explicitly requested.
- Core strategy remains independent of a single competition adapter.

