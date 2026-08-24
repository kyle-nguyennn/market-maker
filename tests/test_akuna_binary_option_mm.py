import pytest

from exchanges.akuna.akuna_binary_option_mm import (
    AJARAI_UNDERLYING_ID,
    FED_FUNDS_RATE_UNDERLYING_ID,
    THERIODIC_UNDERLYING_ID,
    BinaryOption,
    MarketMaker,
    OptionLeg,
    Underlying,
)


def _underlyings(ajarai_value: float) -> list[Underlying]:
    return [
        Underlying("FED", FED_FUNDS_RATE_UNDERLYING_ID, 2.0),
        Underlying("AJR", AJARAI_UNDERLYING_ID, ajarai_value),
        Underlying("THR", THERIODIC_UNDERLYING_ID, 600.0),
    ]


def test_step_advance_credits_expiring_buys_and_sells_once() -> None:
    winning_option = BinaryOption((OptionLeg(AJARAI_UNDERLYING_ID, 1.0),), 1, 1, 100.0)
    losing_option = BinaryOption((OptionLeg(AJARAI_UNDERLYING_ID, 1.0),), 2, 1, 200.0)
    market_maker = MarketMaker(_underlyings(150.0), [winning_option, losing_option], 100.0)

    market_maker.on_trade(winning_option, 0.20, 3, 1)
    market_maker.on_trade(winning_option, 0.20, -2, 1)
    market_maker.on_trade(losing_option, 0.40, 4, 1)
    market_maker.on_trade(losing_option, 0.40, -5, 1)
    assert market_maker.shadow_cash == pytest.approx(93.20)

    expired_options = [winning_option.advance_step(), losing_option.advance_step()]
    market_maker.on_step_advance(_underlyings(150.0), expired_options)
    assert market_maker.shadow_cash == pytest.approx(101.20)
    assert market_maker.position.option_quantity_by_option_id == {}

    market_maker.on_step_advance(_underlyings(150.0), expired_options)
    assert market_maker.shadow_cash == pytest.approx(101.20)