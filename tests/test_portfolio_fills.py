"""Offline idempotent trade-fill tests for paper replay and REST/WS overlap."""
from decimal import Decimal as D

import pytest

from portfolio.accounting import PortfolioState


def test_duplicate_fill_id_never_double_debits_cash_or_inventory():
    p = PortfolioState()
    assert p.apply_fill(side="BUY", price=D("100"), size=D("0.5"),
                        fee=D("0.01"), fill_id="par:wallet:BTC:fill-1")
    assert not p.apply_fill(side="BUY", price=D("100"), size=D("0.5"),
                            fee=D("0.01"), fill_id="par:wallet:BTC:fill-1")
    assert p.position == D("0.5")
    assert p.cash == D("-50.01")
    assert p.fees_paid == D("0.01")
    assert p.seen_fill_ids == {"par:wallet:BTC:fill-1"}


def test_fill_stream_out_of_order_and_maker_rebate():
    p = PortfolioState()
    assert p.apply_fill(side="SELL", price=D("102"), size=D("0.2"),
                        fee=D("-0.02"), fill_id="b")
    assert p.apply_fill(side="BUY", price=D("100"), size=D("0.5"),
                        fee=D("0.01"), fill_id="a")
    assert not p.apply_fill(side="SELL", price=D("102"), size=D("0.2"),
                            fee=D("-0.02"), fill_id="b")
    assert p.position == D("0.3")
    assert p.cash == D("-29.59")
    assert p.fees_paid == D("-0.01")


@pytest.mark.parametrize("kwargs", [
    {"side": "INVALID", "price": D("100"), "size": D("1")},
    {"side": "BUY", "price": D("NaN"), "size": D("1")},
    {"side": "BUY", "price": D("100"), "size": D("-1")},
    {"side": "BUY", "price": D("100"), "size": D("1"), "fee": D("Infinity")},
    {"side": "BUY", "price": D("100"), "size": D("1"), "fill_id": ""},
])
def test_malformed_fill_does_not_mutate_state(kwargs):
    p = PortfolioState()
    with pytest.raises(ValueError):
        p.apply_fill(**kwargs)
    assert p.position == p.cash == p.fees_paid == D("0")
    assert not p.seen_fill_ids
