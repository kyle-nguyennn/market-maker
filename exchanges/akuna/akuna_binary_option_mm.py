import math
import random
from collections import defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final

from .akuna_binary_option_base import (
    AJARAI_UNDERLYING_ID,
    FED_FUNDS_RATE_UNDERLYING_ID,
    THERIODIC_UNDERLYING_ID,
    BinaryOption,
    FokOrder,
    MarketHistory,
    MarketParameters,
    OrderType,
    Position,
    Quote,
    Underlying,
)


# ============================================================================
# YOUR MARKET MAKER -- fill in the six stubbed methods below
# ============================================================================
import logging

@dataclass(frozen=True)
class PricingModel:
    rate_up_probability: float
    rate_down_probability: float
    rate_reversion_strength: float
    rate_step: float
    rate_target: float

    ajr_drift: float
    ajr_rate_beta: float

    thr_drift: float
    thr_rate_beta: float

    ajr_residual_variance: float
    thr_residual_variance: float
    residual_covariance: float


class MarketMaker:
    def __init__(
        self,
        underlying_initial_state: list[Underlying],
        option_initial_state: list[BinaryOption],
        cash_balance: float,
    ) -> None:
        self.underlying_state: list[Underlying] = underlying_initial_state
        self.active_option_state: list[BinaryOption] = option_initial_state
        self.cash_balance: float = cash_balance
        self.position: Position = Position()
        # shallow balance
        self.initial_cash = cash_balance
        self.shadow_cash = cash_balance
        # internal states
        self.estimator = MarketParameterEstimator()
        self.estimated_market_parameters = None
        
        self.pricer = Pricer(
            num_scenarios=100_000,
            seed=20260822,
        )

    def on_step_advance(self, new_underlying_state: list[Underlying], new_option_state: list[BinaryOption]) -> None:
        self.underlying_state = new_underlying_state
        self.active_option_state = new_option_state

    def on_trade(self, option: BinaryOption, price: float, quantity: int, counterparty_id: int) -> None:
        self.position.add_option_quantity(option.option_id, quantity)
        if quantity > 0:
            # We bought.
            self.shadow_cash -= quantity * price
        else:# We sold.
            self.shadow_cash -= (-quantity) * (1.0 - price)

    @property
    def name(self) -> str:  # type: ignore[empty-body]
        # TODO: return a unique display name for your market maker.
        return "kylenz-mm"

    def price_option(self, option: BinaryOption) -> float:  # type: ignore[empty-body]
        # TODO: return your own theoretical probability (in [0, 1]) that `option` expires in
        # the money, using whatever you estimated in `warm_up` and the current
        # `self.underlying_state`. Called whenever you quote or price a FOK, and by the grader
        # to log what you thought an option was worth, so keep it free of side effects.
        if self.estimated_market_parameters is None:
            raise RuntimeError(
                "warm_up() must run before price_option()"
            )

        return self.pricer.price(
            option=option,
            underlying_state=self.underlying_state,
            market_parameters=self.estimated_market_parameters,
        )

    def price_option_from_parameters(  # type: ignore[empty-body]
        self, market_parameters: MarketParameters, option: BinaryOption
    ) -> float:
        # TODO: return the theoretical probability (in [0, 1]) that `option` expires in the
        # money, given `market_parameters` and the current `self.underlying_state`. Only the
        # THEO test calls this, handing you the true parameters; `price_option` above is what
        # prices your live trading.
        return self.pricer.price(
            market_parameters = market_parameters,
            option = option,
            underlying_state = self.underlying_state,
        )

    def quote(self, option: BinaryOption, counterparty_id: int) -> Quote:  # type: ignore[empty-body]
        # TODO: return a two-sided `Quote` for `option`.
        
        theo = self.price_option(option)

        # Defensive clamp.
        theo = max(0.0, min(1.0, theo))

        # Temporary conservative spread: +/- 3 cents.
        edge = 0.03

        # Work in integer pennies so Quote's penny constraint is satisfied.
        bid_cents = math.floor((theo - edge) * 100.0 + 1e-12)
        offer_cents = math.ceil((theo + edge) * 100.0 - 1e-12)

        # Quote requires:
        #
        #   0 <= bid < offer <= 1
        #
        # i.e. cents:
        #
        #   0 <= bid_cents < offer_cents <= 100
        bid_cents = max(0, min(99, bid_cents))
        offer_cents = max(1, min(100, offer_cents))

        # Defensive handling in case rounding collapses the spread.
        if bid_cents >= offer_cents:
            center_cents = round(theo * 100.0)

            if center_cents <= 0:
                bid_cents = 0
                offer_cents = 1

            elif center_cents >= 100:
                bid_cents = 99
                offer_cents = 100

            else:
                bid_cents = center_cents - 1
                offer_cents = center_cents + 1

        return Quote(
            bid_price=bid_cents / 100.0,
            bid_quantity=3,
            offer_price=offer_cents / 100.0,
            offer_quantity=3,
        )

    def respond_to_fok(self, option: BinaryOption, fok_order: FokOrder) -> bool:  # type: ignore[empty-body]
        # TODO: return True to accept the order at its price, False to ignore it. You cannot
        # accept part of it, and accepting does not guarantee you `fok_order.quantity` -- it is
        # shared with every other market maker that accepts. See `FokOrder` for the split rule.
        theo = self.price_option(option)

        if fok_order.order_type == OrderType.BUY:
            # Counterparty buys -> we sell.
            edge = fok_order.price - theo

            capital_required = fok_order.quantity * (1.0 - fok_order.price)

        else:
            # Counterparty sells -> we buy.
            edge = theo - fok_order.price

            capital_required = fok_order.quantity * fok_order.price

        # Model / estimation error buffer.
        if edge < 0.02:
            return False

        expected_edge = (
            edge * fok_order.quantity
        )

        # Don't tie up too much bankroll in one FOK.
        if capital_required > 0.15 * self.shadow_cash:
            return False

        # Maintain a large solvency reserve.
        if self.shadow_cash - capital_required < (
            0.40 * self.initial_cash
        ):
            return False

        # Require worthwhile edge relative to capital.
        if capital_required > 0:
            edge_on_capital = (
                expected_edge / capital_required
            )

            if edge_on_capital < 0.10:
                return False

        return True

    def warm_up(self, market_history: MarketHistory) -> None:
        # TODO: consume the burn-in history (e.g. estimate the market parameters).
        self.estimated_market_parameters = self.estimator.fit(market_history)
    
# Helper classes

class Pricer:
    """
    Theoretical pricer for Akuna BinaryOption contracts.

    A BinaryOption pays:

        1{ sum_i w_i * S_i(T) >= strike }

    so its theoretical value is simply the probability of that event.

    Pricing hierarchy
    -----------------
    1. Expired contract:
         exact payoff.

    2. FED-only contract:
         exact dynamic programming over the discrete rate Markov chain.

    3. Single AJR/THR threshold:
         analytic conditional-lognormal pricing, mixed over the exact
         terminal FED distribution.

    4. Zero-strike AJR/THR relative-value spread:
         analytic pricing using the conditional Gaussian distribution
         of the log ratio.

    5. Anything else:
         deterministic Monte Carlo that mirrors MarketParameters,
         including daily cent rounding.

    Notes
    -----
    The analytic AJR/THR formulas ignore intermediate cent rounding.
    The official THEO examples show this approximation is accurate enough
    to reproduce the grader's values essentially exactly.

    The Monte Carlo fallback does include daily cent rounding.
    """

    def __init__(
        self,
        num_scenarios: int = 100_000,
        seed: int = 20260822,
        max_cache_entries: int = 2,
    ) -> None:
        if num_scenarios <= 0:
            raise ValueError("num_scenarios must be positive")

        if max_cache_entries < 0:
            raise ValueError("max_cache_entries must be non-negative")

        self.num_scenarios = num_scenarios
        self.seed = seed
        self._max_cache_entries = max_cache_entries

        # Cache terminal Monte Carlo scenarios.
        #
        # key:
        #   (
        #       MarketParameters,
        #       ((underlying_id, value), ...),
        #       steps_until_expiry,
        #   )
        #
        # value:
        #   (
        #       (terminal_rate, terminal_ajr, terminal_thr),
        #       ...
        #   )
        self._scenario_cache: dict[
            tuple[Any, tuple[tuple[int, float], ...], int],
            tuple[tuple[float, float, float], ...],
        ] = {}

    # ==================================================================
    # Public interface
    # ==================================================================

    def price(
        self,
        option: BinaryOption,
        underlying_state: list[Underlying],
        market_parameters: MarketParameters,
    ) -> float:
        """
        Return P(option expires with payoff 1).
        """

        values = self._values_by_underlying_id(underlying_state)

        # --------------------------------------------------------------
        # Expired option
        # --------------------------------------------------------------

        if option.steps_until_expiry == 0:
            return option.expiry_valuation(values)

        # --------------------------------------------------------------
        # FED-only option
        # --------------------------------------------------------------

        if self._is_rate_only(option):
            return self._price_rate_only(
                option=option,
                current_rate=values[FED_FUNDS_RATE_UNDERLYING_ID],
                market_parameters=market_parameters,
            )

        # --------------------------------------------------------------
        # Single AJR or THR
        #
        # Examples:
        #
        #   AJR >= 500
        #   THR >= 650
        # --------------------------------------------------------------

        if self._is_single_company(option):
            return self._price_single_company(
                option=option,
                values=values,
                market_parameters=market_parameters,
            )

        # --------------------------------------------------------------
        # AJR/THR zero-strike spread
        #
        # Examples:
        #
        #   THR - AJR >= 0
        #   2 AJR - THR >= 0
        # --------------------------------------------------------------

        if self._is_company_zero_strike_spread(option):
            return self._price_company_zero_strike_spread(
                option=option,
                values=values,
                market_parameters=market_parameters,
            )

        # --------------------------------------------------------------
        # Generic fallback
        # --------------------------------------------------------------

        return self._price_generic_monte_carlo(
            option=option,
            underlying_state=underlying_state,
            market_parameters=market_parameters,
        )

    def clear_cache(self) -> None:
        """
        Clear cached Monte Carlo scenarios.

        Good to call after on_step_advance(), although the state itself
        is already part of the cache key.
        """
        self._scenario_cache.clear()

    # ==================================================================
    # Normal distribution helpers
    # ==================================================================

    @staticmethod
    def _normal_cdf(x: float) -> float:
        """
        Standard normal CDF.

        Using erfc is slightly better numerically in the tails than
        writing 0.5 * (1 + erf(...)).
        """
        return 0.5 * math.erfc(
            -x / math.sqrt(2.0)
        )

    # ==================================================================
    # Exact FED pricing
    # ==================================================================

    def _price_rate_only(
        self,
        option: BinaryOption,
        current_rate: float,
        market_parameters: MarketParameters,
    ) -> float:
        """
        Exact valuation of a FED-only binary.

        Computes the complete terminal rate distribution, then evaluates
        the binary payoff at every possible terminal rate.
        """

        terminal_dist = self._terminal_rate_distribution(
            current_rate=current_rate,
            steps=option.steps_until_expiry,
            market_parameters=market_parameters,
        )

        price = 0.0

        for terminal_rate, probability in terminal_dist.items():
            observable = sum(
                leg.weight * terminal_rate
                for leg in option.legs
            )

            if observable >= option.strike:
                price += probability

        return self._clamp_probability(price)

    def _terminal_rate_distribution(
        self,
        current_rate: float,
        steps: int,
        market_parameters: MarketParameters,
    ) -> dict[float, float]:
        """
        Exact Markov-chain DP.

        dist[r] = P(FED = r after current number of steps)
        """

        dist: dict[float, float] = {
            current_rate: 1.0
        }

        for _ in range(steps):
            next_dist: dict[float, float] = defaultdict(float)

            for rate_value, state_probability in dist.items():
                (
                    up_probability,
                    down_probability,
                ) = market_parameters.tilted_rate_probabilities(
                    rate_value
                )

                stay_probability = (
                    1.0
                    - up_probability
                    - down_probability
                )

                rate_up = market_parameters.next_rate_value(
                    rate_value,
                    +1,
                )

                rate_down = market_parameters.next_rate_value(
                    rate_value,
                    -1,
                )

                next_dist[rate_up] += (
                    state_probability
                    * up_probability
                )

                next_dist[rate_down] += (
                    state_probability
                    * down_probability
                )

                next_dist[rate_value] += (
                    state_probability
                    * stay_probability
                )

            dist = dict(next_dist)

        return dist

    # ==================================================================
    # Single-company analytic pricing
    # ==================================================================

    def _price_single_company(
        self,
        option: BinaryOption,
        values: dict[int, float],
        market_parameters: MarketParameters,
    ) -> float:
        """
        Price:

            1{ w * S_T >= K }

        for S = AJR or THR.

        Conditional on terminal FED R_T = r:

            log(S_T) ~ Normal(mean_log(r), variance)

        because

            sum_t Delta R_t = R_T - R_0.

        We then mix the conditional binary probabilities over the exact
        terminal FED distribution.
        """

        leg = option.legs[0]
        steps = option.steps_until_expiry

        current_rate = values[
            FED_FUNDS_RATE_UNDERLYING_ID
        ]

        # --------------------------------------------------------------
        # Select company parameters
        # --------------------------------------------------------------

        if leg.underlying_id == AJARAI_UNDERLYING_ID:
            current_value = values[
                AJARAI_UNDERLYING_ID
            ]

            drift = market_parameters.ajarai_drift
            rate_beta = market_parameters.ajarai_rate_beta
            sector_beta = market_parameters.ajarai_sector_beta
            idio_std = market_parameters.ajarai_idio_std_dev

        elif leg.underlying_id == THERIODIC_UNDERLYING_ID:
            current_value = values[
                THERIODIC_UNDERLYING_ID
            ]

            drift = market_parameters.theriodic_drift
            rate_beta = market_parameters.theriodic_rate_beta
            sector_beta = market_parameters.theriodic_sector_beta
            idio_std = market_parameters.theriodic_idio_std_dev

        else:
            raise ValueError(
                "Single-company pricer requires AJR or THR"
            )

        if current_value <= 0:
            raise ValueError(
                "Company value must be positive for lognormal pricing"
            )

        # --------------------------------------------------------------
        # Convert:
        #
        #     weight * S >= strike
        #
        # into either:
        #
        #     S >= threshold
        #
        # or
        #
        #     S <= threshold
        # --------------------------------------------------------------

        weight = leg.weight

        if weight > 0:
            threshold = option.strike / weight

            # Since S_T > 0, any nonpositive lower threshold is
            # satisfied with probability one.
            if threshold <= 0:
                return 1.0

            upper_tail = True

        else:
            threshold = option.strike / weight

            # Dividing by a negative weight flips the inequality:
            #
            #     S_T <= threshold
            #
            # But S_T > 0.
            if threshold <= 0:
                return 0.0

            upper_tail = False

        # --------------------------------------------------------------
        # Exact terminal FED distribution
        # --------------------------------------------------------------

        terminal_rate_dist = self._terminal_rate_distribution(
            current_rate=current_rate,
            steps=steps,
            market_parameters=market_parameters,
        )

        # --------------------------------------------------------------
        # Conditional variance does NOT depend on terminal FED.
        #
        # log S_T contains:
        #
        #     sector_beta * sum Z_t
        #     + sum epsilon_t
        #
        # so:
        #
        # Var(log S_T | R_T)
        #
        #   = T * [
        #       (sector_beta * sector_std)^2
        #       + idio_std^2
        #     ]
        # --------------------------------------------------------------

        variance = steps * (
            (
                sector_beta
                * market_parameters.sector_std_dev
            ) ** 2
            + idio_std ** 2
        )

        std_dev = math.sqrt(
            max(variance, 0.0)
        )

        log_threshold = math.log(threshold)

        price = 0.0

        # --------------------------------------------------------------
        # Mixture over terminal FED states
        # --------------------------------------------------------------

        for (
            terminal_rate,
            rate_probability,
        ) in terminal_rate_dist.items():

            mean_log = (
                math.log(current_value)
                + steps * drift
                + rate_beta
                * (terminal_rate - current_rate)
            )

            if std_dev == 0.0:
                terminal_value = math.exp(mean_log)

                if upper_tail:
                    conditional_probability = (
                        1.0
                        if terminal_value >= threshold
                        else 0.0
                    )
                else:
                    conditional_probability = (
                        1.0
                        if terminal_value <= threshold
                        else 0.0
                    )

            elif upper_tail:
                # P(log S >= log K)
                #
                # = Phi(
                #     (mean_log - log K) / sigma
                #   )
                z = (
                    mean_log - log_threshold
                ) / std_dev

                conditional_probability = (
                    self._normal_cdf(z)
                )

            else:
                # P(log S <= log K)
                z = (
                    log_threshold - mean_log
                ) / std_dev

                conditional_probability = (
                    self._normal_cdf(z)
                )

            price += (
                rate_probability
                * conditional_probability
            )

        return self._clamp_probability(price)

    # ==================================================================
    # AJR / THR zero-strike spread analytic pricing
    # ==================================================================

    def _price_company_zero_strike_spread(
        self,
        option: BinaryOption,
        values: dict[int, float],
        market_parameters: MarketParameters,
    ) -> float:
        """
        Price a two-company zero-strike binary with opposite signs:

            w_A * AJR_T + w_H * THR_T >= 0

        Examples:

            THR_T - AJR_T >= 0

            2 AJR_T - THR_T >= 0

        Since the weights have opposite signs, this can be rewritten as
        a ratio condition.

        Example:

            THR_T - AJR_T >= 0

        iff

            log(THR_T) - log(AJR_T) >= 0.

        Conditional on terminal FED, that log ratio is Gaussian.
        """

        steps = option.steps_until_expiry

        current_rate = values[
            FED_FUNDS_RATE_UNDERLYING_ID
        ]

        current_ajr = values[
            AJARAI_UNDERLYING_ID
        ]

        current_thr = values[
            THERIODIC_UNDERLYING_ID
        ]

        if current_ajr <= 0 or current_thr <= 0:
            raise ValueError(
                "Company values must be positive for log-ratio pricing"
            )

        weight_by_id = {
            leg.underlying_id: leg.weight
            for leg in option.legs
        }

        w_ajr = weight_by_id[
            AJARAI_UNDERLYING_ID
        ]

        w_thr = weight_by_id[
            THERIODIC_UNDERLYING_ID
        ]

        # Preconditions should already be guaranteed by dispatcher.
        if w_ajr * w_thr >= 0:
            raise ValueError(
                "Company spread requires opposite-sign weights"
            )

        # --------------------------------------------------------------
        # Exact terminal FED distribution
        # --------------------------------------------------------------

        terminal_rate_dist = self._terminal_rate_distribution(
            current_rate=current_rate,
            steps=steps,
            market_parameters=market_parameters,
        )

        # --------------------------------------------------------------
        # Individual conditional log variances
        # --------------------------------------------------------------

        var_ajr = steps * (
            (
                market_parameters.ajarai_sector_beta
                * market_parameters.sector_std_dev
            ) ** 2
            + market_parameters.ajarai_idio_std_dev ** 2
        )

        var_thr = steps * (
            (
                market_parameters.theriodic_sector_beta
                * market_parameters.sector_std_dev
            ) ** 2
            + market_parameters.theriodic_idio_std_dev ** 2
        )

        # --------------------------------------------------------------
        # Covariance due to shared sector shock.
        #
        # Idiosyncratic shocks are independent.
        # --------------------------------------------------------------

        covariance = steps * (
            market_parameters.ajarai_sector_beta
            * market_parameters.theriodic_sector_beta
            * market_parameters.sector_std_dev ** 2
        )

        # Var(log AJR - log THR)
        variance_diff = (
            var_ajr
            + var_thr
            - 2.0 * covariance
        )

        std_diff = math.sqrt(
            max(variance_diff, 0.0)
        )

        price = 0.0

        # --------------------------------------------------------------
        # Case 1:
        #
        #     +w_A * AJR - |w_H| * THR >= 0
        #
        # therefore:
        #
        #     AJR / THR >= |w_H| / w_A
        # --------------------------------------------------------------

        if w_ajr > 0 and w_thr < 0:
            ratio_threshold = (
                -w_thr / w_ajr
            )

            log_ratio_threshold = math.log(
                ratio_threshold
            )

            for (
                terminal_rate,
                rate_probability,
            ) in terminal_rate_dist.items():

                mean_ajr = (
                    math.log(current_ajr)
                    + steps
                    * market_parameters.ajarai_drift
                    + market_parameters.ajarai_rate_beta
                    * (
                        terminal_rate
                        - current_rate
                    )
                )

                mean_thr = (
                    math.log(current_thr)
                    + steps
                    * market_parameters.theriodic_drift
                    + market_parameters.theriodic_rate_beta
                    * (
                        terminal_rate
                        - current_rate
                    )
                )

                mean_log_ratio = (
                    mean_ajr - mean_thr
                )

                if std_diff == 0.0:
                    conditional_probability = (
                        1.0
                        if mean_log_ratio
                        >= log_ratio_threshold
                        else 0.0
                    )

                else:
                    z = (
                        mean_log_ratio
                        - log_ratio_threshold
                    ) / std_diff

                    conditional_probability = (
                        self._normal_cdf(z)
                    )

                price += (
                    rate_probability
                    * conditional_probability
                )

        # --------------------------------------------------------------
        # Case 2:
        #
        #     +w_H * THR - |w_A| * AJR >= 0
        #
        # therefore:
        #
        #     THR / AJR >= |w_A| / w_H
        # --------------------------------------------------------------

        elif w_ajr < 0 and w_thr > 0:
            ratio_threshold = (
                -w_ajr / w_thr
            )

            log_ratio_threshold = math.log(
                ratio_threshold
            )

            for (
                terminal_rate,
                rate_probability,
            ) in terminal_rate_dist.items():

                mean_ajr = (
                    math.log(current_ajr)
                    + steps
                    * market_parameters.ajarai_drift
                    + market_parameters.ajarai_rate_beta
                    * (
                        terminal_rate
                        - current_rate
                    )
                )

                mean_thr = (
                    math.log(current_thr)
                    + steps
                    * market_parameters.theriodic_drift
                    + market_parameters.theriodic_rate_beta
                    * (
                        terminal_rate
                        - current_rate
                    )
                )

                mean_log_ratio = (
                    mean_thr - mean_ajr
                )

                if std_diff == 0.0:
                    conditional_probability = (
                        1.0
                        if mean_log_ratio
                        >= log_ratio_threshold
                        else 0.0
                    )

                else:
                    z = (
                        mean_log_ratio
                        - log_ratio_threshold
                    ) / std_diff

                    conditional_probability = (
                        self._normal_cdf(z)
                    )

                price += (
                    rate_probability
                    * conditional_probability
                )

        else:
            raise ValueError(
                "Unexpected company spread weight configuration"
            )

        return self._clamp_probability(price)

    # ==================================================================
    # Generic Monte Carlo fallback
    # ==================================================================

    def _price_generic_monte_carlo(
        self,
        option: BinaryOption,
        underlying_state: list[Underlying],
        market_parameters: MarketParameters,
    ) -> float:
        """
        Generic fallback for options not covered analytically.

        This reproduces the official daily simulation process including
        daily cent rounding.
        """

        scenarios = self._terminal_scenarios(
            underlying_state=underlying_state,
            steps=option.steps_until_expiry,
            market_parameters=market_parameters,
        )

        num_itm = 0

        for (
            terminal_rate,
            terminal_ajr,
            terminal_thr,
        ) in scenarios:

            observable = (
                self._observable_from_terminal_state(
                    option=option,
                    rate_value=terminal_rate,
                    ajarai_value=terminal_ajr,
                    theriodic_value=terminal_thr,
                )
            )

            if observable >= option.strike:
                num_itm += 1

        return (
            num_itm
            / len(scenarios)
        )

    # ==================================================================
    # Monte Carlo scenario generation
    # ==================================================================

    def _terminal_scenarios(
        self,
        underlying_state: list[Underlying],
        steps: int,
        market_parameters: MarketParameters,
    ) -> tuple[tuple[float, float, float], ...]:

        values = self._values_by_underlying_id(
            underlying_state
        )

        initial_rate = values[
            FED_FUNDS_RATE_UNDERLYING_ID
        ]

        initial_ajr = values[
            AJARAI_UNDERLYING_ID
        ]

        initial_thr = values[
            THERIODIC_UNDERLYING_ID
        ]

        state_key = tuple(
            sorted(
                (
                    underlying.underlying_id,
                    underlying.value,
                )
                for underlying
                in underlying_state
            )
        )

        cache_key = (
            market_parameters,
            state_key,
            steps,
        )

        cached = self._scenario_cache.get(
            cache_key
        )

        if cached is not None:
            return cached

        # Private deterministic RNG:
        # do NOT perturb grader/global random state.
        rng = random.Random(
            self.seed
            + 1_000_003 * steps
        )

        scenarios: list[
            tuple[float, float, float]
        ] = []

        for _ in range(self.num_scenarios):
            rate_value = initial_rate
            ajr_value = initial_ajr
            thr_value = initial_thr

            for _ in range(steps):
                (
                    rate_value,
                    ajr_value,
                    thr_value,
                ) = self._advance_market_one_step(
                    rate_value=rate_value,
                    ajarai_value=ajr_value,
                    theriodic_value=thr_value,
                    market_parameters=market_parameters,
                    rng=rng,
                )

            scenarios.append(
                (
                    rate_value,
                    ajr_value,
                    thr_value,
                )
            )

        result = tuple(scenarios)

        if self._max_cache_entries > 0:
            while (
                len(self._scenario_cache)
                >= self._max_cache_entries
            ):
                oldest_key = next(
                    iter(self._scenario_cache)
                )

                del self._scenario_cache[
                    oldest_key
                ]

            self._scenario_cache[
                cache_key
            ] = result

        return result

    def _advance_market_one_step(
        self,
        rate_value: float,
        ajarai_value: float,
        theriodic_value: float,
        market_parameters: MarketParameters,
        rng: random.Random,
    ) -> tuple[float, float, float]:
        """
        Mirror MarketParameters.advance_step():

            1. advance FED
            2. calculate FED change
            3. sample one shared sector shock
            4. sample AJR idiosyncratic shock
            5. update AJR
            6. sample THR idiosyncratic shock
            7. update THR

        Company values are rounded to cents each day, matching the
        supplied simulator.
        """

        # --------------------------------------------------------------
        # FED
        # --------------------------------------------------------------

        (
            up_probability,
            down_probability,
        ) = market_parameters.tilted_rate_probabilities(
            rate_value
        )

        draw = rng.random()

        if draw < up_probability:
            next_rate = (
                market_parameters.next_rate_value(
                    rate_value,
                    +1,
                )
            )

        elif (
            draw
            < up_probability
            + down_probability
        ):
            next_rate = (
                market_parameters.next_rate_value(
                    rate_value,
                    -1,
                )
            )

        else:
            next_rate = rate_value

        rate_change = round(
            next_rate - rate_value,
            2,
        )

        # --------------------------------------------------------------
        # Shared sector factor
        # --------------------------------------------------------------

        sector_shock = rng.gauss(
            0.0,
            market_parameters.sector_std_dev,
        )

        # --------------------------------------------------------------
        # AJR
        # --------------------------------------------------------------

        ajr_idio_shock = rng.gauss(
            0.0,
            market_parameters.ajarai_idio_std_dev,
        )

        ajr_log_return = (
            market_parameters.ajarai_drift
            + market_parameters.ajarai_rate_beta
            * rate_change
            + market_parameters.ajarai_sector_beta
            * sector_shock
            + ajr_idio_shock
        )

        next_ajr = round(
            ajarai_value
            * math.exp(ajr_log_return),
            2,
        )

        # --------------------------------------------------------------
        # THR
        # --------------------------------------------------------------

        thr_idio_shock = rng.gauss(
            0.0,
            market_parameters.theriodic_idio_std_dev,
        )

        thr_log_return = (
            market_parameters.theriodic_drift
            + market_parameters.theriodic_rate_beta
            * rate_change
            + market_parameters.theriodic_sector_beta
            * sector_shock
            + thr_idio_shock
        )

        next_thr = round(
            theriodic_value
            * math.exp(thr_log_return),
            2,
        )

        return (
            next_rate,
            next_ajr,
            next_thr,
        )

    # ==================================================================
    # Contract / state helpers
    # ==================================================================

    @staticmethod
    def _is_rate_only(
        option: BinaryOption,
    ) -> bool:
        return all(
            leg.underlying_id
            == FED_FUNDS_RATE_UNDERLYING_ID
            for leg in option.legs
        )

    @staticmethod
    def _is_single_company(
        option: BinaryOption,
    ) -> bool:
        return (
            len(option.legs) == 1
            and option.legs[0].underlying_id
            in {
                AJARAI_UNDERLYING_ID,
                THERIODIC_UNDERLYING_ID,
            }
        )

    @staticmethod
    def _is_company_zero_strike_spread(
        option: BinaryOption,
    ) -> bool:
        if len(option.legs) != 2:
            return False

        if abs(option.strike) > 1e-12:
            return False

        ids = {
            leg.underlying_id
            for leg in option.legs
        }

        if ids != {
            AJARAI_UNDERLYING_ID,
            THERIODIC_UNDERLYING_ID,
        }:
            return False

        w1 = option.legs[0].weight
        w2 = option.legs[1].weight

        return w1 * w2 < 0

    @staticmethod
    def _observable_from_terminal_state(
        option: BinaryOption,
        rate_value: float,
        ajarai_value: float,
        theriodic_value: float,
    ) -> float:
        observable = 0.0

        for leg in option.legs:
            if (
                leg.underlying_id
                == FED_FUNDS_RATE_UNDERLYING_ID
            ):
                value = rate_value

            elif (
                leg.underlying_id
                == AJARAI_UNDERLYING_ID
            ):
                value = ajarai_value

            elif (
                leg.underlying_id
                == THERIODIC_UNDERLYING_ID
            ):
                value = theriodic_value

            else:
                raise ValueError(
                    "Unknown underlying id: "
                    f"{leg.underlying_id}"
                )

            observable += (
                leg.weight * value
            )

        return observable

    @staticmethod
    def _values_by_underlying_id(
        underlying_state: list[Underlying],
    ) -> dict[int, float]:
        values = {
            underlying.underlying_id:
                underlying.value
            for underlying
            in underlying_state
        }

        required = {
            FED_FUNDS_RATE_UNDERLYING_ID,
            AJARAI_UNDERLYING_ID,
            THERIODIC_UNDERLYING_ID,
        }

        missing = (
            required
            - set(values.keys())
        )

        if missing:
            raise ValueError(
                "Missing underlying state(s): "
                f"{sorted(missing)}"
            )

        return values

    @staticmethod
    def _clamp_probability(
        probability: float,
    ) -> float:
        return min(
            max(probability, 0.0),
            1.0,
        )
        
import math


class MarketParameterEstimator:
    """
    Estimate pricing-relevant MarketParameters from MarketHistory.

    V1:
      - FED parameters from linear regression on up/down indicators
      - company drift/rate beta from OLS
      - residual covariance mapped into an equivalent one-factor model
    """

    def __init__(self, rate_step=0.25, rate_target=2.0):
        self.rate_step = rate_step
        self.rate_target = rate_target

    def fit(self, history):
        rate = list(
            history.values_by_underlying_id[
                FED_FUNDS_RATE_UNDERLYING_ID
            ]
        )
        ajr = list(
            history.values_by_underlying_id[
                AJARAI_UNDERLYING_ID
            ]
        )
        thr = list(
            history.values_by_underlying_id[
                THERIODIC_UNDERLYING_ID
            ]
        )

        if not (len(rate) == len(ajr) == len(thr)):
            raise ValueError("Historical series lengths do not match")

        if len(rate) < 3:
            raise ValueError("Not enough history")

        # ----------------------------------------------------------
        # FED
        # ----------------------------------------------------------

        (
            rate_up_probability,
            rate_down_probability,
            rate_reversion_strength,
        ) = self._fit_rate(rate)

        # ----------------------------------------------------------
        # Daily FED changes
        # ----------------------------------------------------------

        delta_rate = [
            round(rate[i + 1] - rate[i], 2)
            for i in range(len(rate) - 1)
        ]

        # ----------------------------------------------------------
        # Company log returns
        # ----------------------------------------------------------

        ajr_returns = [
            math.log(ajr[i + 1] / ajr[i])
            for i in range(len(ajr) - 1)
        ]

        thr_returns = [
            math.log(thr[i + 1] / thr[i])
            for i in range(len(thr) - 1)
        ]

        (
            ajr_drift,
            ajr_rate_beta,
            ajr_residuals,
        ) = self._fit_regression(
            delta_rate,
            ajr_returns,
        )

        (
            thr_drift,
            thr_rate_beta,
            thr_residuals,
        ) = self._fit_regression(
            delta_rate,
            thr_returns,
        )

        # ----------------------------------------------------------
        # Residual covariance matrix
        # ----------------------------------------------------------

        var_ajr, var_thr, cov = self._covariance(
            ajr_residuals,
            thr_residuals,
        )

        # ----------------------------------------------------------
        # Construct any factor decomposition reproducing that matrix.
        #
        # We don't need to recover the hidden sector parameters
        # individually. The Pricer only cares about:
        #
        #   Var(AJR residual)
        #   Var(THR residual)
        #   Cov(AJR residual, THR residual)
        # ----------------------------------------------------------

        (
            sector_std,
            ajr_sector_beta,
            thr_sector_beta,
            ajr_idio_std,
            thr_idio_std,
        ) = self._factorize(
            var_ajr,
            var_thr,
            cov,
        )

        return MarketParameters(
            ajarai_drift=ajr_drift,
            ajarai_idio_std_dev=ajr_idio_std,
            ajarai_rate_beta=ajr_rate_beta,
            ajarai_sector_beta=ajr_sector_beta,

            rate_down_probability=rate_down_probability,
            rate_reversion_strength=rate_reversion_strength,
            rate_up_probability=rate_up_probability,

            sector_std_dev=sector_std,

            theriodic_drift=thr_drift,
            theriodic_idio_std_dev=thr_idio_std,
            theriodic_rate_beta=thr_rate_beta,
            theriodic_sector_beta=thr_sector_beta,

            rate_step=self.rate_step,
            rate_target=self.rate_target,
        )

    # ==============================================================
    # Rate model
    # ==============================================================
    def _fit_rate(self, rates):
        """
        Maximum-likelihood fit of:

            rate_up_probability
            rate_down_probability
            rate_reversion_strength

        using the exact MarketParameters transition model, including
        probability clipping and the zero-rate floor.
        """

        # ----------------------------------------------------------
        # Coarse search
        # ----------------------------------------------------------

        best = None
        best_ll = float("-inf")

        # Reasonably broad initial grid.
        #
        # These are only starting points; refinement below makes the
        # final estimate much finer.
        for p_up_i in range(1, 20):
            p_up = p_up_i * 0.05

            for p_down_i in range(1, 20):
                p_down = p_down_i * 0.05

                if p_up + p_down > 1.0:
                    continue

                for kappa_i in range(0, 11):
                    kappa = kappa_i * 0.05

                    ll = self._rate_score(
                        rates,
                        p_up,
                        p_down,
                        kappa,
                    )

                    if ll > best_ll:
                        best_ll = ll
                        best = (
                            p_up,
                            p_down,
                            kappa,
                        )

        # ----------------------------------------------------------
        # Local refinement
        # ----------------------------------------------------------

        p_up, p_down, kappa = best

        steps = [
            0.02,
            0.01,
            0.005,
            0.002,
            0.001,
        ]

        for step in steps:
            improved = True

            while improved:
                improved = False

                current_best = (
                    p_up,
                    p_down,
                    kappa,
                )

                current_ll = best_ll

                for du in (-step, 0.0, step):
                    for dd in (-step, 0.0, step):
                        for dk in (-step, 0.0, step):

                            if (
                                du == 0.0
                                and dd == 0.0
                                and dk == 0.0
                            ):
                                continue

                            candidate_up = p_up + du
                            candidate_down = p_down + dd
                            candidate_kappa = kappa + dk

                            if candidate_up <= 0.0:
                                continue

                            if candidate_down <= 0.0:
                                continue

                            if (
                                candidate_up
                                + candidate_down
                                > 1.0
                            ):
                                continue

                            if not (
                                0.0
                                <= candidate_kappa
                                <= 1.0
                            ):
                                continue

                            ll = self._rate_score(
                                rates,
                                candidate_up,
                                candidate_down,
                                candidate_kappa,
                            )

                            if ll > current_ll:
                                current_ll = ll

                                current_best = (
                                    candidate_up,
                                    candidate_down,
                                    candidate_kappa,
                                )

                                improved = True

                if improved:
                    (
                        p_up,
                        p_down,
                        kappa,
                    ) = current_best

                    best_ll = current_ll

        # Final defensive projection.
        eps = 1e-6

        p_up = max(eps, min(1.0 - eps, p_up))
        p_down = max(eps, min(1.0 - eps, p_down))

        if p_up + p_down > 1.0:
            total = p_up + p_down

            scale = (
                (1.0 - eps)
                / total
            )

            p_up *= scale
            p_down *= scale

        kappa = max(
            0.0,
            min(1.0, kappa),
        )

        return (
            p_up,
            p_down,
            kappa,
        )
        
    def _rate_score(self,    rates,    p_up,    p_down,    kappa,):
        ll = self._rate_log_likelihood(
            rates,
            p_up,
            p_down,
            kappa,
        )

        n_transitions = len(rates) - 1
        rate_span = max(rates) - min(rates)
        unique_rates = len(set(rates[:-1]))

        # More data / more state-space coverage => trust likelihood more.
        information = (
            n_transitions
            * max(rate_span / self.rate_step, 1.0)
            * max(unique_rates, 1)
        )

        # Strong prior when information is low.
        prior_strength = 40.0 / max(information, 1.0)

        prior_up = 0.25
        prior_down = 0.20
        prior_kappa = 0.10

        penalty = prior_strength * (
            4.0 * (p_up - prior_up) ** 2
            + 4.0 * (p_down - prior_down) ** 2
            + 2.0 * (kappa - prior_kappa) ** 2
        )

        return ll - penalty
    
        
    def _rate_log_likelihood(self,rates,p_up_base,p_down_base,kappa):
        ll = 0.0

        for i in range(len(rates) - 1):
            current = rates[i]
            observed_next = rates[i + 1]

            probability = self._rate_transition_probability(
                current,
                observed_next,
                p_up_base,
                p_down_base,
                kappa,
            )

            # Impossible transition under candidate model.
            if probability <= 0.0:
                return float("-inf")

            ll += math.log(probability)

        return ll
    
    def _rate_transition_probability(self, current, observed_next, p_up_base, p_down_base, kappa):
        """
        Exact transition probability under Akuna's rate model.
        """

        tilt = (
            kappa
            * (self.rate_target - current)
        )

        p_up = min(
            max(
                p_up_base + tilt,
                0.0,
            ),
            1.0,
        )

        p_down = min(
            max(
                p_down_base - tilt,
                0.0,
            ),
            1.0 - p_up,
        )

        p_stay = (
            1.0
            - p_up
            - p_down
        )

        up_rate = max(
            round(
                current + self.rate_step,
                2,
            ),
            0.0,
        )

        down_rate = max(
            round(
                current - self.rate_step,
                2,
            ),
            0.0,
        )

        current = round(current, 2)
        observed_next = round(
            observed_next,
            2,
        )

        probability = 0.0

        # Important: at FED=0, a DOWN event also leaves the
        # observable rate at 0, so probabilities must be added.
        if observed_next == up_rate:
            probability += p_up

        if observed_next == down_rate:
            probability += p_down

        if observed_next == current:
            probability += p_stay

        return probability
    # ==============================================================
    # Company model
    # ==============================================================

    def _fit_regression(self, delta_rate, returns):
        n = len(returns)

        if n == 0:
            raise ValueError("No returns")

        # Stronger shrinkage for short histories.
        #
        # For n=10, meaningful.
        # For n=45+, relatively mild.
        scale = 10.0 / max(n, 1)

        lambda_mu = 2000.0 * scale
        lambda_beta = 2.0 * scale

        sx = sum(delta_rate)
        sy = sum(returns)

        sxx = sum(x * x for x in delta_rate)
        sxy = sum(
            x * y
            for x, y in zip(delta_rate, returns)
        )

        # Normal equations for:
        #
        # [ n + lambda_mu       sx            ] [mu  ] = [sy ]
        # [ sx                  sxx+lambda_b  ] [beta]   [sxy]
        #
        a = n + lambda_mu
        b = sx
        c = sx
        d = sxx + lambda_beta

        det = a * d - b * c

        if abs(det) < 1e-15:
            drift = 0.0
            rate_beta = 0.0
        else:
            drift = (
                sy * d - b * sxy
            ) / det

            rate_beta = (
                a * sxy - c * sy
            ) / det

        residuals = [
            y - drift - rate_beta * x
            for x, y in zip(delta_rate, returns)
        ]

        return drift, rate_beta, residuals

    # ==============================================================
    # Covariance
    # ==============================================================

    def _covariance(self, a, b):
        n = len(a)

        if n != len(b):
            raise ValueError("Residual lengths do not match")

        if n == 0:
            raise ValueError("No residuals")

        mean_a = self._mean(a)
        mean_b = self._mean(b)

        # MLE denominator is fine here: we care about predictive
        # distribution, not an unbiased textbook estimator.
        var_a = sum(
            (x - mean_a) ** 2
            for x in a
        ) / n

        var_b = sum(
            (x - mean_b) ** 2
            for x in b
        ) / n

        cov = sum(
            (x - mean_a) * (y - mean_b)
            for x, y in zip(a, b)
        ) / n

        return var_a, var_b, cov

    # ==============================================================
    # Equivalent one-factor decomposition
    # ==============================================================

    def _factorize(self, var_a, var_b, cov):
        """
        Need:

            var_a = beta_a^2 * sector_std^2 + idio_a^2
            var_b = beta_b^2 * sector_std^2 + idio_b^2
            cov   = beta_a * beta_b * sector_std^2

        Many decompositions work.

        Pick sector_std = 1 and construct a symmetric allocation of
        correlated variance.
        """

        eps = 1e-15

        var_a = max(var_a, 0.0)
        var_b = max(var_b, 0.0)

        if var_a < eps or var_b < eps:
            return (
                1.0,
                0.0,
                0.0,
                math.sqrt(var_a),
                math.sqrt(var_b),
            )

        max_cov = math.sqrt(var_a * var_b)

        # Force estimated covariance matrix to remain PSD.
        cov = max(
            -max_cov,
            min(max_cov, cov),
        )

        rho = cov / max_cov

        abs_rho = abs(rho)

        sector_std = 1.0

        beta_a = math.sqrt(
            abs_rho * var_a
        )

        beta_b = math.sqrt(
            abs_rho * var_b
        )

        if cov < 0:
            beta_b = -beta_b

        idio_var_a = (
            var_a - beta_a * beta_a
        )

        idio_var_b = (
            var_b - beta_b * beta_b
        )

        idio_a = math.sqrt(
            max(idio_var_a, 0.0)
        )

        idio_b = math.sqrt(
            max(idio_var_b, 0.0)
        )

        return (
            sector_std,
            beta_a,
            beta_b,
            idio_a,
            idio_b,
        )

    # ==============================================================
    # Generic math helpers
    # ==============================================================

    def _linear_regression(self, x, y):
        """
        y = intercept + slope*x
        """

        if len(x) != len(y):
            raise ValueError("Regression lengths do not match")

        if not x:
            raise ValueError("Empty regression")

        mean_x = self._mean(x)
        mean_y = self._mean(y)

        var_x = sum(
            (value - mean_x) ** 2
            for value in x
        )

        if var_x < 1e-15:
            return mean_y, 0.0

        cov_xy = sum(
            (xi - mean_x)
            * (yi - mean_y)
            for xi, yi in zip(x, y)
        )

        slope = cov_xy / var_x

        intercept = (
            mean_y
            - slope * mean_x
        )

        return intercept, slope

    @staticmethod
    def _mean(values):
        return sum(values) / len(values)