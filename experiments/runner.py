from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any


@dataclass(frozen=True)
class ExperimentConfig:
    strategy_names: list[str]
    parameter_grid: dict[str, list[Any]] = field(default_factory=dict)
    seeds: list[int] = field(default_factory=lambda: [0])
    iterations: int = 1


def parameter_sweep(config: ExperimentConfig) -> list[dict[str, Any]]:
    """Construct a deterministic parameter grid for experiment runs.

    This is intentionally simple and venue-agnostic so strategies can be evaluated
    consistently across fixed baselines before an exchange adapter is added.
    """
    scenarios: list[dict[str, Any]] = []

    parameter_names = list(config.parameter_grid.keys())
    parameter_values: list[tuple[Any, ...]] = [
        tuple(values) for values in (config.parameter_grid[name] for name in parameter_names)
    ]

    if not parameter_names:
        parameter_names = ["__default__"]
        parameter_values = [tuple([None])]

    for strategy_name in config.strategy_names:
        for seed in config.seeds:
            for values in product(*parameter_values):
                params = dict(zip(parameter_names, values))
                for iteration in range(config.iterations):
                    scenarios.append(
                        {
                            "strategy": strategy_name,
                            "seed": seed,
                            "iteration": iteration,
                            **params,
                        }
                    )

    return scenarios
