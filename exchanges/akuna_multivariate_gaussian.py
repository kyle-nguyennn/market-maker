from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class AkunaUnderlyingGeneratorConfig:
    seed: int = 42
    n_steps: int = 250
    initial_levels: tuple[float, float, float] = (5.0, 100.0, 100.0)
    mean_drift: tuple[float, float, float] = (0.0005, 0.0010, 0.0010)
    covariance: tuple[tuple[float, float, float], ...] = (
        (0.0008, -0.0003, -0.0003),
        (-0.0003, 0.0025, 0.0012),
        (-0.0003, 0.0012, 0.0030),
    )
    common_factor_loading: tuple[float, float, float] = (0.6, 0.8, 0.9)
    common_factor_volatility: float = 0.015
    idiosyncratic_volatility: tuple[float, float, float] = (0.012, 0.018, 0.020)


class AkunaUnderlyingSeriesGenerator:
    """Synthetic 3-underlying generator for the Akuna challenge.

    This models the three latent drivers as a correlated log-normal-like process:
    - Fed funds rate
    - Ajarai valuation
    - Theori valuation

    The generator is intentionally simple and deterministic given a seed so it can
    be used to validate pricing and quoting logic before the real challenge API is
    available.
    """

    def __init__(self, config: Optional[AkunaUnderlyingGeneratorConfig] = None) -> None:
        self.config = config or AkunaUnderlyingGeneratorConfig()
        self.rng = np.random.default_rng(self.config.seed)

    @property
    def names(self) -> tuple[str, str, str]:
        return ("fed_funds_rate", "ajarai_valuation", "theori_valuation")

    def _diffusion_matrix(self) -> np.ndarray:
        covariance = np.array(self.config.covariance, dtype=float)
        if covariance.shape != (3, 3):
            raise ValueError("covariance must be a 3x3 matrix")
        return covariance

    def generate_levels(self) -> np.ndarray:
        n_steps = self.config.n_steps
        initial = np.array(self.config.initial_levels, dtype=float)
        drift = np.array(self.config.mean_drift, dtype=float)
        cov = self._diffusion_matrix()
        factor_loadings = np.array(self.config.common_factor_loading, dtype=float)
        idio_vol = np.array(self.config.idiosyncratic_volatility, dtype=float)

        common_factor = np.empty(n_steps, dtype=float)
        common_factor[0] = 0.0
        for i in range(1, n_steps):
            common_factor[i] = 0.5 * common_factor[i - 1] + self.rng.normal(0.0, self.config.common_factor_volatility)

        shocks = self.rng.multivariate_normal(
            mean=np.zeros(3, dtype=float),
            cov=cov,
            size=n_steps,
        )

        path = np.empty((n_steps, 3), dtype=float)
        path[0] = initial
        for i in range(1, n_steps):
            common_component = common_factor[i] * factor_loadings
            idiosyncratic_component = self.rng.normal(0.0, 1.0, size=3) * idio_vol
            log_returns = drift + shocks[i] + common_component + idiosyncratic_component
            path[i] = path[i - 1] * np.exp(log_returns)

        return path

    def generate_returns(self) -> np.ndarray:
        levels = self.generate_levels()
        if len(levels) < 2:
            return np.zeros((0, 3), dtype=float)
        return np.diff(levels, axis=0) / levels[:-1]

    def generate_market_snapshot(self, step: int = -1) -> dict[str, float]:
        path = self.generate_levels()
        row = path[step]
        return {
            "fed_funds_rate": float(row[0]),
            "ajarai_valuation": float(row[1]),
            "theori_valuation": float(row[2]),
        }
