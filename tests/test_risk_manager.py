"""Risk tests run offline against final, venue-neutral Decimal intents."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from model.market import BookLevel, MarketState
from model.order import OrderIntent, Side
from risk.risk_manager import (
    OpenExposure, RiskLimits, RiskManager, RiskSnapshot,
)


def limits(**overrides):
    args = dict(
        max_abs_position=D("2"), max_order_notional=D("1000"),
        max_abs_notional=D("400"), max_drawdown=D("150"),
        max_market_data_age_ms=2000, min_order_notional=D("10"),
        max_gross_notional=D("600"), max_net_notional=D("500"),
        max_quote_deviation_bps=D("200"),
    )
    args.update(overrides)
    return RiskLimits(**args)


def market(ts=10000, *, valid=True, bid="100", ask="101"):
    return MarketState(
        instrument="BTC", timestamp_ms=ts,
        bids=(BookLevel(D(bid), D("10")),),
        asks=(BookLevel(D(ask), D("10")),), is_valid=valid,
        invalid_reason=None if valid else "crossed_book",
    )


def snap(**overrides):
    data = dict(
        market=market(), now_ms=11000, dex="par", expected_dex="par",
        allowed_instruments=frozenset({"BTC"}), positions={"BTC": D("0")},
        marks={"BTC": D("100")}, fillable_orders=(),
        account_equity=D("1000"), baseline_equity=D("1000"),
        margin_mode="standard", account_id="0x" + "a" * 40,
        expected_account_id="0x" + "a" * 40, capacity_buy={"BTC": D("10")},
        capacity_sell={"BTC": D("10")},
    )
    data.update(overrides)
    return RiskSnapshot(**data)


def order(side=Side.BID, price="99.9", size="0.2", **kw):
    return OrderIntent("BTC", side, D(price), D(size), **kw)


def check(candidate=None, context=None, risk=None):
    risk = risk or RiskManager(limits())
    return risk.evaluate_placement(candidate or order(), context or snap())


def test_conservative_bid_and_ask_are_permitted():
    assert check().allowed
    assert check(order(Side.ASK, "101.1")).allowed


@pytest.mark.parametrize("problem, context, reason", [
    ("stale", snap(market=market(ts=1)), "stale_market_data"),
    ("future", snap(market=market(ts=12000)), "future_market_data"),
    ("invalid", snap(market=market(valid=False)), "crossed_book"),
    ("crossed", snap(market=market(bid="101", ask="100")), "invalid_book"),
    ("wrong dex", snap(dex="pa"), "wrong_dex"),
    ("wrong account", snap(account_id="0x" + "b" * 40), "wrong_or_unverified_account"),
    ("wrong mode", snap(margin_mode=None), "unknown_margin_mode"),
    ("missing equity", snap(account_equity=None), "unverified_account_equity"),
    ("missing capacity", snap(capacity_buy=None), "capacity_unavailable"),
    ("insufficient capacity", snap(capacity_buy={"BTC": D("0")}), "insufficient_capacity"),
    ("missing mark", snap(marks={}), "missing_mark_price"),
])
def test_fail_closed_pretrade(problem, context, reason):
    assert check(context=context).reason == reason


def test_final_post_only_quote_must_remain_passive():
    assert check(order(price="101")).reason == "post_only_would_cross"
    assert check(order(Side.ASK, price="100")).reason == "post_only_would_cross"
    assert check(order(post_only=False)).reason == "unsupported_order_type"


def test_nan_negative_zero_and_notional():
    for price, size in (("NaN", "1"), ("0", "1"), ("1", "-1")):
        assert not check(order(price=price, size=size)).allowed
    assert check(order(size="11")).reason == "max_order_notional"


def test_price_collar_and_minimum_notional():
    assert check(order(price="80")).reason == "price_collar"
    assert check(order(size="0.05")).reason == "min_order_notional"


def test_existing_position_and_all_resting_plus_pending_bids_are_counted():
    existing = (
        OpenExposure("BTC", Side.BID, D("0.3"), "0x1"),
        OpenExposure("BTC", Side.BID, D("0.1"), "0x2"),
    )
    context = snap(positions={"BTC": D("1.5")}, fillable_orders=existing)
    assert check(order(size="0.2"), context).reason == "max_abs_position"


def test_cancel_pending_remains_fillable_and_cloid_not_double_counted():
    pending = OpenExposure("BTC", Side.BID, D("1.95"), "0xA")
    candidate = order(size="0.2", client_order_id="0xB")
    assert check(candidate, snap(fillable_orders=(pending,))).reason == "max_abs_position"
    reserved = OpenExposure("BTC", Side.BID, D("0.2"), "0xB")
    assert check(candidate, snap(fillable_orders=(reserved,))).allowed


def test_reduce_only_allowed_when_reducing_long_not_increasing_it():
    context = snap(positions={"BTC": D("1.9")})
    assert check(order(Side.BID, "99.9", "0.2"), context).reason == "max_abs_position"
    assert check(order(Side.ASK, "101.1", "0.2", reduce_only=True), context).allowed
    assert check(order(Side.BID, "99.9", "0.2", reduce_only=True), context).reason == "invalid_reduce_only"


def test_under_ten_exception_only_for_full_reduce_only_close():
    context = snap(positions={"BTC": D("0.05")})
    full = order(Side.ASK, "101.1", "0.05", reduce_only=True)
    partial = order(Side.ASK, "101.1", "0.04", reduce_only=True)
    assert check(full, context).allowed
    assert check(partial, context).reason == "min_order_notional"


def test_drawdown_latches_kill_until_authorized_clean_reset():
    risk = RiskManager(limits())
    assert risk.evaluate_placement(order(), snap(account_equity=D("849"))).reason == "drawdown_kill_switch"
    assert risk.kill_switch
    assert risk.evaluate_placement(order(), snap()).reason == "kill_switch"
    with pytest.raises(PermissionError):
        risk.reset_kill_switch(operator_authorized=False, state_reconciled=True)
    with pytest.raises(PermissionError):
        risk.reset_kill_switch(operator_authorized=True, state_reconciled=False)
    risk.reset_kill_switch(operator_authorized=True, state_reconciled=True)
    assert risk.evaluate_placement(order(), snap()).allowed


def test_no_silent_infinite_limits_from_null_configuration():
    risk = RiskManager(RiskLimits(D("2"), D("1000"), D("400"), D("150"), 2000))
    assert risk.evaluate_placement(order(), snap()).reason == "risk_config_incomplete"


def test_aggregate_cross_market_exposure_requires_verified_marks():
    context = snap(positions={"BTC": D("0"), "ETH": D("1")}, marks={"BTC": D("100"), "ETH": D("1000")})
    assert check(context=context).reason == "max_gross_notional"
    assert check(context=replace(context, marks={"BTC": D("100")})).reason == "missing_mark_price"
