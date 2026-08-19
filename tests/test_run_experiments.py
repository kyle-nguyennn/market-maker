from experiments.runner import ExperimentConfig
from run_experiments import run_experiments


def test_run_experiments_generates_results_for_multiple_strategies_and_iterations():
    config = ExperimentConfig(
        strategy_names=["fixed", "midpoint"],
        parameter_grid={
            "spread_ticks": [2],
            "base_price": [50],
            "inventory": [0, 5],
        },
        seeds=[0],
        iterations=2,
    )

    results = run_experiments(config)

    assert len(results) == 8
    assert {result.strategy for result in results} == {"fixed", "midpoint"}
    assert {result.seed for result in results} == {0}
    assert {result.iteration for result in results} == {0, 1}
