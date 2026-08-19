from quoter.base_quoter import Side
from quoter.baseline_models import FixedQuoteStrategy, MidpointMMQuoter, InventoryAwareMMQuoter, VolatilityAwareMMQuoter
from market_handler.market_adapter import MarketState
from experiments.runner import ExperimentConfig, parameter_sweep


def test_midpoint_and_inventory_quotes_are_ordered_and_inventory_sensitive():
    state = MarketState(
        instrument="AJARAI_UP",
        bid_prices=[48, 46],
        ask_prices=[52, 55],
        bid_volumes=[10, 12],
        ask_volumes=[9, 11],
        inventory=5,
        is_valid=True,
    )

    midpoint = MidpointMMQuoter(spread_ticks=4)
    midpoint_quotes = midpoint.quote(state)
    assert midpoint_quotes[0].side == Side.BID
    assert midpoint_quotes[1].side == Side.ASK
    assert midpoint_quotes[0].price < midpoint_quotes[1].price

    inventory = InventoryAwareMMQuoter(spread_ticks=4, inventory_penalty=0.08)
    inv_quotes = inventory.quote(state)
    assert inv_quotes[0].price <= midpoint_quotes[0].price
    assert inv_quotes[1].price <= midpoint_quotes[1].price
    assert inv_quotes[0].price < inv_quotes[1].price


def test_volatility_aware_quote_widens_when_volatility_rises():
    state = MarketState(
        instrument="AJARAI_UP",
        bid_prices=[49, 47],
        ask_prices=[51, 54],
        bid_volumes=[10, 9],
        ask_volumes=[11, 12],
        is_valid=True,
    )

    low_vol = VolatilityAwareMMQuoter(spread_ticks=2, volatility=0.1)
    high_vol = VolatilityAwareMMQuoter(spread_ticks=2, volatility=0.4)

    low_quotes = low_vol.quote(state)
    high_quotes = high_vol.quote(state)

    assert (high_quotes[1].price - high_quotes[0].price) >= (low_quotes[1].price - low_quotes[0].price)


def test_experiment_sweep_generates_parameter_sets():
    config = ExperimentConfig(
        strategy_names=["midpoint", "inventory"],
        parameter_grid={"inventory_penalty": [0.0, 0.1]},
        seeds=[1, 2],
    )

    results = parameter_sweep(config)

    assert len(results) == 8
    assert all(result["strategy"] in {"midpoint", "inventory"} for result in results)
    assert all(result["seed"] in {1, 2} for result in results)


def test_fixed_strategy_is_a_simple_reference_quote():
    state = MarketState(
        instrument="AJARAI_UP",
        bid_prices=[50],
        ask_prices=[50],
        bid_volumes=[10],
        ask_volumes=[10],
        is_valid=True,
    )

    fixed = FixedQuoteStrategy(reference_price=50, spread_ticks=4)
    quotes = fixed.quote(state)

    assert quotes[0].price < quotes[1].price
    assert quotes[0].price == 48
    assert quotes[1].price == 52
