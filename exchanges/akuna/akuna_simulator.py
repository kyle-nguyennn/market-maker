"""Akuna simulator adapter backed by a synthetic multivariate Gaussian process."""

from __future__ import annotations

from typing import Iterable

import numpy as np

from exchanges.akuna.akuna_multivariate_gaussian import (
    AkunaUnderlyingGeneratorConfig,
    AkunaUnderlyingSeriesGenerator,
)
from market_handler.market_adapter import MarketState


class AkunaSimulatorAPI:
    def __init__(
        self,
        *,
        seed: int | None = None,
        venue_name: str = "Akuna",
        n_steps: int = 250,
    ) -> None:
        self.seed = seed if seed is not None else 42
        self.venue_name = venue_name
        self.config = AkunaUnderlyingGeneratorConfig(seed=self.seed, n_steps=n_steps)
        self.generator = AkunaUnderlyingSeriesGenerator(self.config)
        self.series = self.generator.generate_levels()

    def contract_payoff(self, contract_name: str, levels: np.ndarray | None = None) -> np.ndarray:
        series = levels if levels is not None else self.series
        if contract_name == "AJARAI_UP":
            return (series[:, 1] > 100.0).astype(float)
        if contract_name == "THEORI_UP":
            return (series[:, 2] > 100.0).astype(float)
        if contract_name == "AJARAI_GT_THEORI":
            return (series[:, 1] > series[:, 2]).astype(float)
        if contract_name == "BOTH_AI_UP":
            return ((series[:, 1] > 100.0) & (series[:, 2] > 100.0)).astype(float)
        if contract_name == "FED_DOWN":
            return (series[:, 0] < 5.0).astype(float)
        raise ValueError(f"Unsupported contract: {contract_name}")

    def get_market_state(self, instrument: str = "AJARAI_UP", step: int = -1) -> MarketState:
        row = self.series[step]
        fed_rate, ajarai, theori = row

        if instrument == "FED_DOWN":
            level = fed_rate
        elif instrument == "THEORI_UP":
            level = theori
        else:
            level = ajarai

        mid = int(round(float(level)))
        spread = 4
        bid = max(1, mid - spread // 2)
        ask = max(bid + 1, mid + spread - spread // 2)

        return MarketState(
            instrument=instrument,
            timestamp=float(step),
            sequence=step,
            bid_prices=[bid, max(1, bid - 2)],
            bid_volumes=[10, 4],
            ask_prices=[ask, min(99, ask + 2)],
            ask_volumes=[8, 5],
            inventory=0,
            is_valid=True,
            last_trade_price=mid,
            last_trade_side="BUY" if step % 2 == 0 else "SELL",
        )

    def stream_market_states(self, instrument: str = "AJARAI_UP") -> Iterable[MarketState]:
        for step in range(len(self.series)):
            yield self.get_market_state(instrument=instrument, step=step)

    def submit_quote(self, *args, **kwargs) -> None:
        return None
