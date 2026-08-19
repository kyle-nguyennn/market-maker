# Akuna 2026 Binary-Options Market-Making Implementation Specification

## 1. Purpose

Integrate an Akuna-specific binary-options strategy into the existing market-making framework without coupling the framework to an assumed challenge API or a single statistical model.

This document is an implementation brief for Codex. It distinguishes:

- Confirmed facts from the competition briefing.
- Working hypotheses that must be verified after opening the challenge.
- Stable framework interfaces.
- Akuna-specific pricing, belief, quoting, and risk logic.
- A staged implementation plan optimized for a 5–8 hour competition window.

The implementation must prioritize, in order:

1. Correct contract interpretation and pricing.
2. Prevention of bankruptcy or hard-limit breaches.
3. Correct position, cash, and fill accounting.
4. Inventory-aware quoting.
5. Cross-contract consistency and portfolio risk.
6. Conservative adaptation to order flow.
7. Model selection and tuning.

Do not turn this into a general-purpose research platform during the competition.

---

## 2. Confirmed Competition Facts

The briefing states that:

- The instruments are binary options/event contracts settling at `0` or `1` according to whether an event occurs.
- There are three underlying variables:
  - Fed funds rate.
  - Valuation of fictional private AI company **Ajarai**.
  - Valuation of fictional private AI company **Theori**.
- The variables move according to a joint distribution that must be estimated from historical returns.
- Generally, falling rates correspond to rising startup valuations and vice versa.
- The bot must:
  - Produce two-sided quotes.
  - Include bid/ask quantities.
  - Respond directly to customer buy/sell orders.
  - Consider theoretical value and current positions.
- Scenarios become progressively harder through stronger competitors and/or harder trading conditions.
- The bot should adapt using information obtained from counterparties.
- Bankruptcy causes automatic failure of a scenario.
- Initial scoring occurs in HackerRank; leading competitors later face each other directly.
- AI assistance is allowed, while original ideas are intentionally rewarded.

---

## 3. Unknowns: Resolve Before Strategy Work

Do not encode assumptions about the following until the challenge documentation or API confirms them:

### 3.1 Contract semantics

- Exact contract definitions and thresholds.
- Whether all contracts pay exactly `0/1` or use another notional.
- Whether YES and NO contracts are separate instruments.
- Settlement time and horizon.
- Whether the event is defined by terminal level, maximum, minimum, average, or another path-dependent condition.
- Whether prices are represented as `[0, 1]`, cents `[0, 100]`, or another scale.

### 3.2 Market mechanics

- Whether the three underlyings themselves are tradable.
- RFQ/customer-response mechanics versus resting limit orders.
- Whether quotes can be cancelled or replaced.
- Whether fills are partial.
- Tick size, lot size, fees, rebates, latency, and quote expiry.
- Whether customer identity persists.
- Whether rejected/accepted quotes provide information.
- Whether competing market makers' quotes or trades are observable.

### 3.3 Risk and scoring

- Exact bankruptcy definition.
- Starting cash and accounting convention.
- Position, order, or notional limits.
- Mark-to-market versus settlement-only bankruptcy.
- Whether open quotes reserve risk capacity.
- Whether score is raw PnL, risk-adjusted PnL, survival-weighted PnL, or scenario rank.

### 3.4 Data-generating process

- Whether historical rows are levels, differences, simple returns, or log returns.
- Whether rows are chronologically ordered.
- Whether settlement is one step or multiple steps ahead.
- Whether observations are IID, serially dependent, regime-switching, or non-stationary.
- Whether the generator is Gaussian, a mixture, an empirical process, or another synthetic distribution.

Add a short `CHALLENGE_DISCOVERY.md` or structured notes section immediately after inspecting the challenge. Record each resolved item before changing strategy code.

---

## 4. Core Strategy Model

For a binary contract `j` with event indicator `g_j(X_T)`:

```text
payoff_j = g_j(X_T) in {0, 1}
fair_value_j = E[g_j(X_T) | available information]
             = P(event_j occurs | available information)
```

The strategy loop is:

```text
historical data + current state
    -> estimated joint terminal-state distribution
    -> binary event probabilities
    -> posterior/order-flow adjustment
    -> inventory-adjusted reservation values
    -> bid/ask spreads and quantities
    -> scenario-level risk checks
    -> quotes
    -> fills/order observations
    -> update cash, positions, and beliefs
```

Expected market-making PnL is conceptually:

```text
spread capture
- adverse selection
- inventory losses
- model error
- transaction costs
```

subject to the hard constraint of avoiding bankruptcy.

---

## 5. Integration Principles

### 5.1 Preserve the existing framework

Do not rewrite existing exchange, order, portfolio, or strategy abstractions unless required by the challenge adapter. Prefer small adapters and new strategy components.

Before editing:

1. Inspect repository instructions and `AGENTS.md`.
2. Identify current abstractions for:
   - Instruments/contracts.
   - Market events and RFQs.
   - Quotes/orders.
   - Fills.
   - Cash and positions.
   - Risk checks.
   - Strategy lifecycle.
   - Configuration.
3. Map the interfaces in this specification onto existing equivalents.
4. Reuse existing components where their semantics match.
5. Do not create duplicate position or cash sources of truth.

### 5.2 Stable statistical boundary

All distribution models must output the same weighted terminal-scenario representation. Pricing, Bayesian updating, quoting, and risk must never depend on whether scenarios came from a Gaussian, bootstrap, VAR, or ML model.

### 5.3 Separate base prediction from order-flow updating

The distribution model estimates:

```text
P(X_T | historical data, current market state)
```

The belief updater modifies scenario weights to approximate:

```text
P(X_T | historical data, state, observed order flow)
```

Do not embed counterparty/order-flow behavior into every distribution model.

### 5.4 Determinism

- Inject random-number generators or seeds.
- Use common random numbers when comparing models or finite-difference sensitivities.
- Ensure identical inputs and seed produce identical quotes.
- Log active model name, parameters, and seed.

---

## 6. Canonical Domain Objects

Adapt names to existing framework conventions rather than duplicating types.

```python
from dataclasses import dataclass
from typing import Callable, Hashable, Protocol
import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]
ContractId = Hashable


@dataclass(frozen=True)
class ScenarioBatch:
    # Shape: (n_scenarios, n_underlyings)
    terminal_states: FloatArray

    # Shape: (n_scenarios,)
    log_weights: FloatArray

    # Stable column ordering, e.g. ("fed_rate", "ajarai", "theori")
    names: tuple[str, ...]

    def normalized_weights(self) -> FloatArray:
        shifted = self.log_weights - np.max(self.log_weights)
        weights = np.exp(shifted)
        return weights / weights.sum()


@dataclass(frozen=True)
class ModelContext:
    current_levels: FloatArray
    recent_history: FloatArray
    horizon: int
    n_scenarios: int


@dataclass(frozen=True)
class BinaryContract:
    contract_id: ContractId
    # Vectorized: (N, D) terminal states -> (N,) payoffs
    payoff: Callable[[FloatArray], FloatArray]


@dataclass(frozen=True)
class ProposedQuote:
    contract_id: ContractId
    bid: float
    ask: float
    bid_size: int
    ask_size: int
```

Required `ScenarioBatch` invariants:

- At least one scenario.
- `terminal_states.shape == (N, D)`.
- `log_weights.shape == (N,)`.
- `len(names) == D`.
- All values and log weights are finite.
- Normalized weights are non-negative and sum to `1`.

Use arrays internally for speed. Avoid one Python object per particle in the hot path.

---

## 7. Distribution Model Interface

```python
class DistributionModel(Protocol):
    name: str

    def fit(self, historical_movements: FloatArray) -> None:
        ...

    def generate(
        self,
        context: ModelContext,
        rng: np.random.Generator,
    ) -> ScenarioBatch:
        ...
```

The challenge strategy may switch models through configuration without changing pricing or quoting code.

### 7.1 Required baseline models

Implement in this order:

1. **EmpiricalBootstrapModel**
   - Samples complete historical rows jointly.
   - Must never sample underlying columns independently.
   - Primary non-parametric baseline.

2. **GaussianModel**
   - Estimates mean vector and covariance matrix.
   - Samples from a multivariate Gaussian.
   - Diagnostic and low-variance parametric baseline.

3. **SmoothedBootstrapModel**
   - Samples complete rows and adds modest multivariate noise.
   - Bandwidth configurable.

4. **VARResidualBootstrapModel** only if chronology and serial dependence exist
   - Fit a small VAR, initially order 1.
   - Bootstrap joint residual vectors.
   - Optional EWMA residual scaling for current volatility.

Do not implement neural density estimation before baselines are correct and validated.

### 7.2 Optional models

Only add when diagnostics and walk-forward validation justify them:

- Regime-conditioned bootstrap.
- Gaussian mixture.
- Empirical-marginal Gaussian copula.
- Direct event classifiers.
- Mixture-density network or normalizing flow.

Any direct contract classifier must be checked for cross-contract probability inconsistencies.

---

## 8. Movement and Horizon Semantics

Keep transformation from simulated movements to terminal levels separate from distribution estimation.

```python
class TerminalStateTransformer(Protocol):
    def to_terminal(
        self,
        current_levels: FloatArray,
        movement_paths: FloatArray,
    ) -> FloatArray:
        ...
```

Supported conventions:

```text
Additive change: X_T = X_0 + sum(delta_X_t)
Simple return:   X_T = X_0 * product(1 + r_t)
Log return:      X_T = X_0 * exp(sum(r_t))
```

Do not assume covariance scales linearly with the horizon unless IID increments are supported by the challenge data.

For multi-step empirical simulation:

- Sample whole cross-sectional rows per step.
- Use block bootstrap if temporal dependence is material.
- Apply the correct compounding convention.

---

## 9. Contract Registry and Pricing

### 9.1 Contract registry

The challenge adapter must translate the challenge's instrument definitions into vectorized payoff predicates.

Example only:

```python
FED, AJARAI, THEORI = 0, 1, 2

contracts = {
    "THEORI_GT_100": BinaryContract(
        contract_id="THEORI_GT_100",
        payoff=lambda x: (x[:, THEORI] > 100.0).astype(float),
    ),
    "BOTH_AI_GT_100": BinaryContract(
        contract_id="BOTH_AI_GT_100",
        payoff=lambda x: (
            (x[:, AJARAI] > 100.0)
            & (x[:, THEORI] > 100.0)
        ).astype(float),
    ),
}
```

Do not hard-code these sample contracts unless they exist in the actual challenge.

### 9.2 Scenario pricer

For contract `j`:

```text
fair_value_j = sum_i weight_i * payoff_j(terminal_state_i)
```

```python
class ScenarioPricer:
    def price(
        self,
        contract: BinaryContract,
        scenarios: ScenarioBatch,
    ) -> float:
        payoffs = contract.payoff(scenarios.terminal_states)
        return float(np.dot(scenarios.normalized_weights(), payoffs))
```

Pricing invariants:

- Every binary price lies in `[0, 1]` before exchange-unit conversion.
- Event plus exact complement equals `1`.
- If `A` implies `B`, then `P(A) <= P(B)`.
- For thresholds `K1 < K2`, `P(X > K1) >= P(X > K2)`.
- `P(A and B) <= min(P(A), P(B))`.
- `P(A or B) >= max(P(A), P(B))`.

The shared scenario distribution should enforce most consistency relationships automatically.

---

## 10. Belief and Counterparty Updating

### 10.1 Baseline behavior

Start with belief updating disabled or very weak. First prove that static pricing, accounting, and risk work.

### 10.2 Particle reweighting

Given observation `y` and likelihood `L_i = P(y | X_T = scenario_i)`:

```text
new_log_weight_i = old_log_weight_i + log(max(L_i, epsilon))
```

Normalize with log-sum-exp.

### 10.3 Customer order likelihood

A simple informed-customer likelihood for a customer buying at ask `a` is:

```text
P(customer buys | X_T) = sigmoid(beta * (payoff(X_T) - a))
```

For a customer selling at bid `b`:

```text
P(customer sells | X_T) = sigmoid(beta * (b - payoff(X_T)))
```

Do not assume every order is informed. Use a mixture:

```text
P(observation | X_T)
= pi_informed * P(observation | X_T, informed)
+ (1 - pi_informed) * P(observation | noise)
```

Conservative starting parameters for experiments:

```text
pi_informed = 0.10 to 0.25
beta = small/moderate
noise_probability = 0.5 unless mechanics imply otherwise
```

These are experimental defaults, not challenge facts.

### 10.4 Counterparty state

If identities persist, maintain per-counterparty state:

```python
@dataclass
class CounterpartyState:
    trade_count: int = 0
    informed_probability: float = 0.20
    toxicity_score: float = 0.0
```

If identities do not persist, maintain only global/recent order-flow state.

Use toxicity first to widen spreads and reduce size. Move fair value aggressively only after evidence supports the likelihood model; otherwise competitors may manipulate the bot.

### 10.5 Degeneracy controls

Monitor effective sample size:

```text
ESS = 1 / sum_i(weight_i^2)
```

If ESS falls below a configured fraction of scenario count:

- Regenerate scenarios from the base distribution and replay retained observations, or
- Resample particles and reset equal weights, if implemented correctly.

Do not silently continue with a posterior supported by only a few particles.

---

## 11. Reservation Value and Quote Construction

### 11.1 Binary payoff variance

For fair probability `p`:

```text
variance = p * (1 - p)
```

### 11.2 Initial inventory adjustment

Use a transparent linear baseline:

```text
reservation_value = fair_value - gamma * position * p * (1 - p)
```

Interpretation:

- Long position lowers reservation value and both quotes.
- Short position raises reservation value and both quotes.

This is an inventory heuristic inspired by reservation-price market making, not a claim that the original Avellaneda–Stoikov closed form applies directly to this challenge.

### 11.3 Half-spread decomposition

```text
half_spread
= base_half_spread
+ model_uncertainty_buffer
+ posterior_uncertainty_buffer
+ counterparty_toxicity_buffer
+ inventory/concentration_buffer
+ transaction_cost_buffer
+ scenario_difficulty_buffer
```

Then:

```text
bid = floor_to_tick(reservation_value - half_spread)
ask = ceil_to_tick(reservation_value + half_spread)
```

Clamp to valid exchange bounds and ensure `bid < ask` after rounding.

### 11.4 Model disagreement

When multiple models are available, compute:

```text
model_range_j = max_m(price_jm) - min_m(price_jm)
```

Use this as a model-risk diagnostic and optionally widen:

```text
model_uncertainty_buffer = eta * model_range_j
```

Do not average models automatically unless validation supports the ensemble.

### 11.5 Quote quantities

Start with fixed small size, reduced by:

- Remaining position capacity.
- Scenario loss capacity.
- Counterparty toxicity.
- Model uncertainty.
- Correlated portfolio concentration.
- Near-bankruptcy state.

Bid and ask sizes need not be symmetric.

---

## 12. Portfolio Accounting and Risk

### 12.1 One source of truth

Reuse the framework portfolio/accounting component. Every fill must update exactly once:

```text
position[contract] += signed_quantity
cash -= signed_quantity * trade_price
```

Required sign convention:

- Positive signed quantity: the bot buys.
- Negative signed quantity: the bot sells.
- Customer buys from the bot: bot quantity is negative.
- Customer sells to the bot: bot quantity is positive.

Convert units/notional according to challenge semantics.

### 12.2 Scenario portfolio value

For scenario `i`:

```text
terminal_portfolio_value_i
= cash
+ sum_j(position_j * payoff_j(scenario_i))
+ underlying_hedge_value_i, if applicable
```

Risk must be calculated at the portfolio level, not by treating correlated contracts independently.

### 12.3 Pre-quote risk reservation

Before returning a quote, evaluate independently:

- Full bid-side fill.
- Full ask-side fill.
- Any simultaneous-fill behavior allowed by the challenge.

Reduce quantity until all hard constraints pass.

If quotes remain live, reserve risk capacity for outstanding orders to avoid exceeding limits through concurrent fills.

### 12.4 Required risk metrics

At minimum calculate:

- Worst supported scenario terminal value.
- Posterior lower-tail quantile/VaR.
- Expected terminal value.
- Gross and net position.
- Contract-level limits.
- Correlated-event concentration.
- Distance to bankruptcy threshold.

Hard risk checks must not be bypassed by quote policy.

### 12.5 Survival buffer

Do not operate exactly at the bankruptcy boundary. Configure a survival buffer for:

- Model misspecification.
- Monte Carlo sampling gaps.
- Multiple/partial fills.
- Price and unit rounding.
- Delayed state updates.

---

## 13. Hedging

### 13.1 Underlying hedge

Only implement if the underlying variables are tradable.

Estimate binary sensitivities through common-random-number finite differences or a scenario minimum-variance hedge. Do not assume a Black–Scholes digital formula matches the synthetic process.

For scenario underlying changes `DeltaU` and option portfolio values `V`, the minimum-variance underlying position is:

```text
h* = -Cov(DeltaU)^(-1) Cov(DeltaU, V)
```

Use ridge regularization for numerical stability.

Do not automatically hedge to zero. Use configurable partial hedging and no-trade bands to control turnover.

### 13.2 Cross-contract hedge

If underlyings are not tradable:

- Use inventory-skewed quotes to encourage natural unwinds.
- Use related binary contracts only when their scenario payoff vectors genuinely offset risk.
- Determine hedge ratios using scenario covariance/regression.
- Continue enforcing scenario-level portfolio constraints because correlation hedges are imperfect.

Hedging is secondary to correct pricing and survival in the initial implementation.

---

## 14. Model Diagnostics and Selection

### 14.1 Immediate diagnostics

After loading historical data, inspect:

- Shape, missing values, duplicates, and ordering.
- Column meanings and return convention.
- Means, standard deviations, quantiles.
- Correlation/covariance matrix.
- Skewness and kurtosis.
- Scatter plots or numeric dependence summaries.
- Autocorrelation of returns and squared returns.
- Rolling volatility and rolling correlation.
- Possible clusters/regimes.

### 14.2 Compare model-implied contract prices

For every actual contract, compare at least:

```text
Gaussian
Empirical bootstrap
Smoothed bootstrap
```

Large disagreement implies meaningful model risk and should initially cause wider quotes/smaller sizes.

### 14.3 Chronological validation

If data is time ordered, use walk-forward validation; do not randomly shuffle.

Score contract-like events using:

```text
Brier score = mean((predicted_probability - realized_outcome)^2)
Log loss, with probability clipping
Calibration by probability bucket
```

Validate probabilities for actual or representative event families, not merely return MSE.

Only retain VAR, EWMA, regime, or ML complexity if it improves out-of-sample event-probability calibration.

---

## 15. Challenge Adapter

All challenge-specific names and callback semantics must be isolated in an adapter.

Responsibilities:

- Parse historical data.
- Parse underlying state.
- Parse contract definitions into payoff predicates.
- Convert challenge price units to internal probability units and back.
- Convert challenge quantity/notional conventions.
- Translate RFQ/customer/order events into internal observations.
- Translate internal proposed quotes into challenge responses.
- Translate fills into the framework's canonical fill event.
- Expose limits, bankruptcy rules, and scenario lifecycle.

The statistical model and strategy must not import HackerRank-specific classes directly.

Suggested interface, adapted to the existing framework:

```python
class AkunaChallengeAdapter(Protocol):
    def load_history(self, raw_input) -> FloatArray: ...
    def current_context(self, raw_state) -> ModelContext: ...
    def build_contract_registry(self, raw_contracts) -> dict[ContractId, BinaryContract]: ...
    def parse_event(self, raw_event): ...
    def encode_quote(self, quote: ProposedQuote): ...
```

---

## 16. Strategy Lifecycle

Suggested lifecycle:

```python
class AkunaBinaryMarketMaker:
    def initialize(self, challenge_data) -> None:
        # Resolve units and semantics.
        # Build contract registry.
        # Fit configured base distribution model(s).
        # Generate initial ScenarioBatch.
        # Initialize portfolio/risk/counterparty state.
        ...

    def on_market_state(self, event) -> None:
        # Update current levels/history.
        # Regenerate scenarios if base predictive state changed materially.
        ...

    def on_customer_request(self, request) -> ProposedQuote:
        # Price contract from current scenarios.
        # Apply inventory reservation adjustment.
        # Set spread and candidate sizes.
        # Apply full-fill portfolio risk checks.
        # Return safe quote.
        ...

    def on_fill(self, fill) -> None:
        # Update canonical portfolio exactly once.
        # Update counterparty statistics.
        # Conservatively reweight belief if enabled.
        # Recalculate related contract values.
        ...

    def on_settlement(self, settlement) -> None:
        # Validate payoff and accounting.
        # Record attribution and calibration outcomes.
        ...
```

---

## 17. Configuration

All parameters must be centralized and serializable.

```python
@dataclass(frozen=True)
class AkunaStrategyConfig:
    model_type: str = "bootstrap"
    n_scenarios: int = 20_000
    seed: int = 42

    base_half_spread: float = 0.03
    inventory_gamma: float = 0.01
    base_size: int = 1
    max_position: int = 100

    model_uncertainty_eta: float = 0.5
    toxicity_spread_scale: float = 0.0

    belief_updates_enabled: bool = False
    initial_informed_probability: float = 0.20
    informed_beta: float = 2.0
    ess_resample_fraction: float = 0.25

    bankruptcy_buffer: float = 0.0  # Must be set after rules are known.

    hedge_enabled: bool = False
    hedge_fraction: float = 0.5
    hedge_no_trade_band: float = 0.0
```

These values are experimental placeholders. Codex must not treat them as tuned defaults.

Support named presets:

```text
safe_baseline
bootstrap_inventory
multi_model_conservative
order_flow_experimental
```

---

## 18. Logging and PnL Attribution

Record enough information to explain every quote and compare variants:

- Timestamp/event number/scenario ID.
- Contract and counterparty identifier if available.
- Base model fair value.
- Posterior fair value.
- Current position.
- Inventory adjustment.
- Each spread buffer.
- Final bid/ask and sizes.
- Risk check results and binding constraint.
- Fill price, side, and quantity.
- Cash and position after fill.
- Effective sample size.
- Model name and active parameters.

PnL attribution where possible:

```text
spread capture
inventory mark/settlement PnL
adverse-selection proxy
hedge PnL
fees
```

Logging must be disableable or lightweight if the competition has strict runtime/output limits.

---

## 19. Tests and Invariants

### 19.1 Scenario/model tests

- Every model emits a valid `ScenarioBatch`.
- Same seed and input produce the same scenarios.
- Bootstrap samples whole rows jointly.
- Terminal transformation matches additive/simple/log-return semantics.
- No NaN or infinite output.

### 19.2 Contract/pricing tests

- Known deterministic events price at `0` or `1`.
- Complementary contracts sum to `1`.
- Threshold monotonicity holds.
- Intersection and union bounds hold.
- Pricing is invariant to scenario permutation with matching weights.

### 19.3 Belief tests

- Weights remain normalized.
- Repeated informative buys increase the relevant event probability under the test likelihood.
- Correlated contracts reprice in the expected direction in a controlled fixture.
- Noise-mixture updates move less than fully informed updates.
- ESS degeneracy is detected.

### 19.4 Quote tests

- Bid is strictly below ask after rounding.
- Quotes stay within exchange bounds.
- Long inventory shifts quotes downward; short inventory shifts upward.
- Size becomes zero at hard position capacity.
- Increased uncertainty/toxicity never narrows the spread.

### 19.5 Accounting/risk tests

- Buy/sell sign convention is correct.
- Fills are applied exactly once.
- Full bid and full ask fills are risk-checked separately.
- Correlated positions are evaluated jointly.
- Unsafe quantity is reduced or rejected.
- Bankruptcy buffer is respected.
- Outstanding-order risk is reserved if quotes persist.

### 19.6 End-to-end simulator tests

Create a minimal local simulator with configurable counterparties:

- Noise trader.
- Value-sensitive trader.
- Informed trader.
- Adversarial/manipulative trader.
- Inventory-motivated trader.

Compare identical seeded runs for:

- Static midpoint strategy.
- Fair-value plus fixed spread.
- Inventory-skewed strategy.
- Belief-updating strategy.
- Optional hedged strategy.

Track PnL, drawdown, bankruptcy frequency, inventory, fill count, spread capture, and calibration.

---

## 20. Staged Delivery Plan

### Stage 0: Repository mapping

- Read repository instructions.
- Identify existing framework interfaces.
- Produce a short mapping from this specification to actual files/classes.
- Do not refactor unrelated framework code.

### Stage 1: Safe static baseline

- Challenge adapter.
- Contract registry.
- Empirical pricing or explicit challenge baseline.
- Fixed spread.
- Small fixed size.
- Correct accounting.
- Hard position and bankruptcy checks.

Exit criterion: runs end-to-end without accounting errors or bankruptcy in basic tests.

### Stage 2: Joint scenario pricing

- `ScenarioBatch`.
- Bootstrap and Gaussian models.
- Scenario pricer.
- Probability consistency tests.
- Model-comparison output.

Exit criterion: actual contracts price coherently from both models.

### Stage 3: Inventory-aware market making

- Reservation-value skew.
- Asymmetric risk-aware sizes.
- Portfolio scenario risk.
- Survival buffer.

Exit criterion: repeated one-sided fills cause orderly de-risking rather than runaway inventory.

### Stage 4: Conditional/time-series model

- Only if historical ordering and validation justify it.
- Add VAR residual bootstrap and optional EWMA scaling.
- Walk-forward Brier/log-loss comparison.

Exit criterion: improves contract-event calibration out of sample.

### Stage 5: Counterparty adaptation

- Conservative likelihood mixture.
- Particle reweighting.
- ESS monitoring.
- Toxicity-based spread/size changes.

Exit criterion: improves informed-flow performance without becoming easily manipulable.

### Stage 6: Hedging and tuning

- Only if instruments/mechanics support it.
- Cross-contract or underlying hedge.
- Partial hedge/no-trade band.
- Seeded parameter sweeps.

Exit criterion: reduces bankruptcy/drawdown or improves score after costs.

---

## 21. Competition-Time Execution Order

When the timer begins:

1. Read all rules and record unknowns from Section 3.
2. Determine exact unit, sign, settlement, and bankruptcy conventions.
3. Run the provided baseline unchanged and capture its score/output.
4. Integrate the adapter and safe static baseline.
5. Verify accounting with tiny deterministic cases.
6. Inspect historical data and actual contract definitions.
7. Compare empirical/bootstrap and Gaussian prices.
8. Add inventory skew and scenario risk.
9. Submit a robust checkpoint early.
10. Add conditional modelling only if supported by diagnostics.
11. Add counterparty updates only after observing scenario behavior.
12. Preserve the best known submission before risky experiments.

At each stage, keep a runnable checkpoint. Do not spend the entire window perfecting model architecture before obtaining a valid score.

---

## 22. Definition of Done

The Akuna integration is complete when:

- Challenge-specific parsing and callbacks are isolated in an adapter.
- Distribution models are switchable through configuration.
- Every model produces the same weighted terminal-scenario abstraction.
- Contracts are vectorized payoff predicates over terminal scenarios.
- Fair values are coherent probabilities in internal `[0, 1]` units.
- Belief updating is optional and separate from base prediction.
- Quotes reflect fair value, inventory, uncertainty, toxicity, and transaction costs where known.
- Bid and ask quantities pass portfolio-level full-fill risk checks.
- Cash, positions, fills, and outstanding risk have one source of truth.
- Bankruptcy protection is hard and tested.
- Model and strategy comparisons are deterministic and measurable.
- A conservative baseline can be submitted independently of advanced models.

---

## 23. Final Guidance to Codex

- Treat confirmed rules as facts and everything else as a hypothesis.
- Prefer the simplest model that prices actual contract events well out of sample.
- Optimize probability calibration and market-making score, not generic return-prediction metrics.
- Preserve cross-contract consistency by pricing from one joint scenario distribution.
- Never sacrifice solvency for a small expected-value improvement.
- Avoid speculative abstractions that do not help produce a valid competition submission.
- Before implementing advanced logic, demonstrate through logs/tests exactly what failure mode it addresses.

