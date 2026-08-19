from market_handler.market_adapter import MarketState
from quoter.options_market_making.akuna_binary_option_quoter import AkunaBinaryOptionMarketMaker, BeliefState


def test_market_state_has_best_bid_and_ask_helpers():
    state = MarketState(
        instrument="AJARAI_UP",
        bid_prices=[45, 42],
        ask_prices=[55, 58],
        bid_volumes=[10, 20],
        ask_volumes=[8, 15],
        is_valid=True,
    )

    assert state.best_bid == 45
    assert state.best_ask == 55
    assert state.mid_price == 50
    assert state.spread == 10


def test_akuna_binary_option_quoter_returns_two_sided_quotes():
    state = MarketState(
        instrument="AJARAI_UP",
        bid_prices=[48, 45],
        ask_prices=[52, 55],
        bid_volumes=[12, 8],
        ask_volumes=[10, 12],
        inventory=2,
        is_valid=True,
    )

    quoter = AkunaBinaryOptionMarketMaker(
        base_probability=0.5,
        spread_ticks=4,
        inventory_penalty=0.04,
        max_quote_size=25,
    )

    quotes = quoter.quote(state)

    assert len(quotes) == 2
    assert quotes[0].side.name == "BID"
    assert quotes[1].side.name == "ASK"
    assert quotes[0].price < quotes[1].price
    assert quotes[0].price < 100
    assert quotes[1].price > 0
    assert quotes[0].qty > 0
    assert quotes[1].qty > 0


def test_belief_state_updates_probability_on_customer_buy():
    belief = BeliefState(probability=0.5, informed_probability=0.2, beta=2.0)

    updated = belief.update("BUY", 0.8)

    assert updated > 0.5
    assert belief.probability > 0.5
