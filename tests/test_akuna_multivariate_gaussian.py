import numpy as np

from exchanges.akuna_multivariate_gaussian import AkunaUnderlyingSeriesGenerator, AkunaUnderlyingGeneratorConfig
from exchanges.akuna_simulator import AkunaSimulatorAPI


def test_generated_series_has_three_correlated_underlyings():
    config = AkunaUnderlyingGeneratorConfig(
        seed=7,
        n_steps=50,
    )
    generator = AkunaUnderlyingSeriesGenerator(config)
    levels = generator.generate_levels()

    assert levels.shape == (50, 3)
    assert np.all(np.isfinite(levels))
    assert levels[0].tolist() == [5.0, 100.0, 100.0]
    assert set(generator.names) == {"fed_funds_rate", "ajarai_valuation", "theori_valuation"}


def test_returns_are_generated_from_levels_and_are_finite():
    config = AkunaUnderlyingGeneratorConfig(seed=13, n_steps=20)
    generator = AkunaUnderlyingSeriesGenerator(config)
    returns = generator.generate_returns()

    assert returns.shape == (19, 3)
    assert np.all(np.isfinite(returns))


def test_market_snapshot_returns_last_state():
    config = AkunaUnderlyingGeneratorConfig(seed=3, n_steps=10)
    generator = AkunaUnderlyingSeriesGenerator(config)
    snapshot = generator.generate_market_snapshot(-1)

    assert set(snapshot.keys()) == {"fed_funds_rate", "ajarai_valuation", "theori_valuation"}
    assert all(np.isfinite(list(snapshot.values())))


def test_market_state_tracks_underlying_value_faithfully():
    sim = AkunaSimulatorAPI(seed=42, n_steps=10)
    row = sim.series[1]

    state = sim.get_market_state("AJARAI_UP", step=1)

    assert state.best_bid <= round(row[1]) <= state.best_ask
    assert abs(state.last_trade_price - round(row[1])) <= 1
    assert abs(state.mid_price - round(row[1])) <= 1
