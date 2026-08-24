from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from exchanges.akuna.akuna_simulator import AkunaSimulatorAPI
from market_handler.market_adapter import MarketState
from experiments.runner import ExperimentConfig, parameter_sweep
from quoter.baseline_models import FixedQuoteStrategy, MidpointMMQuoter, InventoryAwareMMQuoter, VolatilityAwareMMQuoter
from quoter.options_market_making.akuna_binary_option_quoter import AkunaBinaryOptionMarketMaker


MODEL_FACTORIES: dict[str, Callable[..., Any]] = {
    "fixed": FixedQuoteStrategy,
    "midpoint": MidpointMMQuoter,
    "inventory": InventoryAwareMMQuoter,
    "volatility": VolatilityAwareMMQuoter,
    "akuna_binary_option": AkunaBinaryOptionMarketMaker,
}


@dataclass(frozen=True)
class RunResult:
    strategy: str
    seed: int
    iteration: int
    quote_bid: int
    quote_ask: int
    bid_qty: int
    ask_qty: int
    mid_price: int
    inventory: int
    params: dict[str, Any]


@dataclass
class ExperimentRunner:
    config: ExperimentConfig

    def _normalize_strategy_params(self, strategy_name: str, params: dict[str, Any]) -> dict[str, Any]:
        base = dict(params)
        max_qty = base.get("max_qty", base.get("max_quote_size", 10))
        spread_ticks = base.get("spread_ticks", 4)

        if strategy_name == "fixed":
            return {
                "reference_price": base.get("base_price", 50),
                "spread_ticks": spread_ticks,
                "max_qty": max_qty,
            }
        if strategy_name == "midpoint":
            return {
                "spread_ticks": spread_ticks,
                "max_qty": max_qty,
            }
        if strategy_name == "inventory":
            return {
                "spread_ticks": spread_ticks,
                "inventory_penalty": base.get("inventory_penalty", 0.05),
                "max_qty": max_qty,
            }
        if strategy_name == "volatility":
            return {
                "spread_ticks": spread_ticks,
                "volatility": base.get("volatility", 0.2),
                "max_qty": max_qty,
            }
        if strategy_name == "akuna_binary_option":
            return {
                "base_probability": base.get("base_probability", 0.5),
                "spread_ticks": spread_ticks,
                "inventory_penalty": base.get("inventory_penalty", 0.04),
                "max_quote_size": base.get("max_quote_size", 25),
                "informed_probability": base.get("informed_probability", 0.2),
                "beta": base.get("beta", 2.0),
                "noise_probability": base.get("noise_probability", 0.5),
            }
        return dict(base)

    def build_model(self, strategy_name: str, params: dict[str, Any]) -> Any:
        factory = MODEL_FACTORIES.get(strategy_name)
        if factory is None:
            raise ValueError(f"Unsupported strategy: {strategy_name}")
        return factory(**self._normalize_strategy_params(strategy_name, params))

    def build_market_state(self, seed: int, iteration: int, params: dict[str, Any]) -> MarketState:
        inventory = params.get("inventory", 0)
        base_price = params.get("base_price", 50)
        spread = params.get("spread_ticks", 4)
        best_bid = max(1, base_price - spread // 2)
        best_ask = max(best_bid + 1, base_price + spread - spread // 2)
        return MarketState(
            instrument="AJARAI_UP",
            timestamp=float(seed + iteration),
            sequence=iteration,
            bid_prices=[best_bid, max(1, best_bid - 2)],
            bid_volumes=[10, 4],
            ask_prices=[best_ask, min(99, best_ask + 2)],
            ask_volumes=[8, 5],
            inventory=inventory,
            is_valid=True,
        )

    def run_single(self, strategy_name: str, seed: int, iteration: int, params: dict[str, Any]) -> RunResult:
        model = self.build_model(strategy_name, params)
        state = self.build_market_state(seed, iteration, params)
        quotes = model.quote(state)
        bid = quotes[0]
        ask = quotes[1]
        return RunResult(
            strategy=strategy_name,
            seed=seed,
            iteration=iteration,
            quote_bid=bid.price,
            quote_ask=ask.price,
            bid_qty=bid.qty,
            ask_qty=ask.qty,
            mid_price=state.mid_price,
            inventory=state.inventory,
            params=params,
        )

    def run(self) -> list[RunResult]:
        results: list[RunResult] = []
        runs = parameter_sweep(self.config)
        for run in runs:
            strategy_name = run["strategy"]
            seed = run["seed"]
            iteration = run["iteration"]
            params = {k: v for k, v in run.items() if k not in {"strategy", "seed", "iteration"}}
            results.append(self.run_single(strategy_name, seed, iteration, params))
        return results


def run_experiments(config: ExperimentConfig) -> list[RunResult]:
    return ExperimentRunner(config).run()


def run_akuna_stream(
    strategy_name: str = "akuna_binary_option",
    *,
    seed: int = 42,
    n_steps: int = 20,
    instrument: str = "AJARAI_UP",
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    simulator = AkunaSimulatorAPI(seed=seed, n_steps=n_steps)
    model_params = params or {
        "base_probability": 0.5,
        "spread_ticks": 4,
        "inventory_penalty": 0.04,
        "max_quote_size": 25,
        "informed_probability": 0.2,
        "beta": 2.0,
        "noise_probability": 0.5,
    }
    model = MODEL_FACTORIES[strategy_name](**ExperimentRunner(config=ExperimentConfig([], {}))._normalize_strategy_params(strategy_name, model_params))
    snapshots: list[dict[str, Any]] = []
    for step, state in enumerate(simulator.stream_market_states(instrument=instrument)):
        quotes = model.quote(state)
        snapshots.append(
            {
                "step": step,
                "instrument": instrument,
                "state": state,
                "bid": quotes[0],
                "ask": quotes[1],
                "event_probability": simulator.generator.generate_market_snapshot(step=step)["ajarai_valuation"],
            }
        )
    return snapshots


if __name__ == "__main__":
    config = ExperimentConfig(
        strategy_names=["fixed", "midpoint", "inventory", "volatility", "akuna_binary_option"],
        parameter_grid={
            "spread_ticks": [2, 4],
            "inventory_penalty": [0.0, 0.05],
            "volatility": [0.1, 0.3],
            "base_probability": [0.3, 0.5],
            "max_quote_size": [10, 20],
            "base_price": [50],
            "inventory": [0, 5],
        },
        seeds=[0, 1],
        iterations=2,
    )
    results = run_experiments(config)
    for result in results[:10]:
        print(
            result.strategy,
            result.seed,
            result.iteration,
            result.quote_bid,
            result.quote_ask,
            result.inventory,
            result.params,
        )

    stream = run_akuna_stream(strategy_name="akuna_binary_option", seed=42, n_steps=5)
    print("\nSynthetic Akuna stream preview:")
    for sample in stream:
        print(
            f"step={sample['step']} bid={sample['bid'].price} ask={sample['ask'].price} "
            f"mid={sample['state'].mid_price}"
        )
