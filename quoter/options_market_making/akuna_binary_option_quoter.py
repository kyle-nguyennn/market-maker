from __future__ import annotations

import math

from quoter.base_quoter import BaseMMQuoter, Quote, Side
from market_handler.market_adapter import MarketState


class BeliefState:
    """Posterior probability for a binary event.

    This implements the section 10 baseline: a simple informed-customer update
    that raises the event probability when the customer buys and reduces it when
    the customer sells. The state stays intentionally simple and challenge-agnostic.
    """

    def __init__(
        self,
        probability: float = 0.5,
        informed_probability: float = 0.2,
        beta: float = 2.0,
        noise_probability: float = 0.5,
    ) -> None:
        self.probability = float(min(1.0, max(0.0, probability)))
        self.informed_probability = float(min(1.0, max(0.0, informed_probability)))
        self.beta = float(beta)
        self.noise_probability = float(min(1.0, max(0.0, noise_probability)))

    def _sigmoid(self, x: float) -> float:
        return 1.0 / (1.0 + math.exp(-x))

    def _customer_likelihood(self, order_side: str, observed_price: float) -> tuple[float, float]:
        if order_side.upper() == "BUY":
            informed = self._sigmoid(self.beta * (1.0 - observed_price))
            noise = self.noise_probability
        elif order_side.upper() == "SELL":
            informed = self._sigmoid(self.beta * (observed_price - 0.0))
            noise = 1.0 - self.noise_probability
        else:
            informed = self.noise_probability
            noise = self.noise_probability

        return informed, noise

    def update(self, order_side: str, observed_price: float) -> float:
        informed_likelihood, noise_likelihood = self._customer_likelihood(order_side, observed_price)
        likelihood = (
            self.informed_probability * informed_likelihood
            + (1.0 - self.informed_probability) * noise_likelihood
        )

        # Bayes update with a simple Bernoulli prior.
        posterior_numerator = likelihood * self.probability
        posterior_denominator = posterior_numerator + (1.0 - likelihood) * (1.0 - self.probability)
        self.probability = posterior_denominator and posterior_numerator / posterior_denominator
        return self.probability


class AkunaBinaryOptionMarketMaker(BaseMMQuoter):
    """Minimal market maker for Akuna binary option contracts.

    The challenge-specific contract semantics are intentionally left behind an
    adapter boundary. This class prices the event in a probability-like space
    and translates that into a two-sided quote using the general quoter pipeline:
    fair value -> reservation price -> spread -> size.
    """

    def __init__(
        self,
        base_probability: float = 0.5,
        spread_ticks: int = 4,
        inventory_penalty: float = 0.04,
        max_quote_size: int = 25,
        price_floor: int = 1,
        price_ceiling: int = 99,
        informed_probability: float = 0.2,
        beta: float = 2.0,
        noise_probability: float = 0.5,
    ) -> None:
        self.base_probability = max(0.0, min(1.0, base_probability))
        self.spread_ticks = max(1, int(spread_ticks))
        self.inventory_penalty = max(0.0, float(inventory_penalty))
        self.max_quote_size = max(1, int(max_quote_size))
        self.price_floor = max(0, int(price_floor))
        self.price_ceiling = max(self.price_floor + 1, int(price_ceiling))
        self.belief = BeliefState(
            probability=self.base_probability,
            informed_probability=informed_probability,
            beta=beta,
            noise_probability=noise_probability,
        )

    def _clamp_price(self, price: int) -> int:
        return max(self.price_floor, min(self.price_ceiling, int(price)))

    def fair_price(self, state: MarketState) -> int:
        if not state.is_valid:
            return self._clamp_price(int(round(self.belief.probability * 100)))

        if state.bid_prices and state.ask_prices:
            fair = state.mid_price
            if fair > 0:
                belief_price = int(round(self.belief.probability * 100))
                return self._clamp_price(max(fair, belief_price))

        return self._clamp_price(int(round(self.belief.probability * 100)))

    def reservation_price(self, state: MarketState, fair: int) -> int:
        inventory_skew = state.inventory * self.inventory_penalty * 100.0
        reservation = fair - inventory_skew
        return self._clamp_price(int(round(reservation)))

    def calculate_spread(self, state: MarketState, fair: int, reservation: int) -> tuple[int, int]:
        half_spread = max(1, self.spread_ticks // 2)
        full_spread = max(half_spread * 2, self.spread_ticks)
        ask_offset = max(half_spread, full_spread - half_spread)
        return half_spread, ask_offset

    def calculate_size(self, state: MarketState, bid_price: int, ask_price: int) -> tuple[int, int]:
        inventory_bias = max(1, self.max_quote_size - abs(state.inventory))
        size = max(1, min(self.max_quote_size, inventory_bias))
        return size, size

    def record_customer_flow(self, order_side: str, observed_price: float) -> float:
        return self.belief.update(order_side, observed_price)

    def quote(self, state: MarketState):
        if not state.is_valid:
            return [Quote(0, 0, Side.BID), Quote(0, 0, Side.ASK)]
        return super().quote(state)
