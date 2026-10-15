"""Offline mocked SDK boundary tests: live=True here uses a FAKE client only."""
from decimal import Decimal as D

import pytest

from execution.order_manager import OrderManager, OrderState
from execution.safe_gateway import SafeExecutionGateway
from exchanges.hyperliquid.config import HyperliquidVenueConfig
from exchanges.hyperliquid.types import HyperliquidMarketSpec, VenueOrderResult
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter
from model.market import BookLevel, MarketState
from model.order import OrderEvent, OrderEventType as E, OrderIntent, Side
from risk.risk_manager import RiskLimits, RiskManager, RiskSnapshot


ADDRESS = "0x" + "a" * 40


class FakeClient:
    def __init__(self):
        self.placements = []
        self.cancels = []
        self.failure = None

    def place_order(self, request):
        self.placements.append(request)
        if self.failure:
            raise self.failure
        return VenueOrderResult("resting", order_id=71)

    def cancel_order(self, instrument, *, client_order_id):
        self.cancels.append((instrument, client_order_id))
        if self.failure:
            raise self.failure
        return True


def context(**overrides):
    market = MarketState(
        "BTC", 10000, (BookLevel(D("100"), D("10")),),
        (BookLevel(D("101"), D("10")),),
    )
    data = dict(
        market=market, now_ms=11000, dex="par", expected_dex="par",
        allowed_instruments=frozenset({"BTC"}), positions={"BTC": D("0")},
        marks={"BTC": D("100")}, fillable_orders=(),
        account_equity=D("1000"), baseline_equity=D("1000"),
        margin_mode="standard", capacity_buy={"BTC": D("10")},
        capacity_sell={"BTC": D("10")},
        account_id=ADDRESS, expected_account_id=ADDRESS,
    )
    data.update(overrides)
    return RiskSnapshot(**data)


def setup(*, live=False, authorized=False):
    manager = OrderManager(session_id="gateway-test", dex="par", account_id=ADDRESS)
    manager.resynchronize([], positions_reconciled=True, fills_reconciled=True)
    risk = RiskManager(RiskLimits(
        max_abs_position=D("2"), max_order_notional=D("1000"),
        max_abs_notional=D("400"), max_drawdown=D("150"),
        max_market_data_age_ms=2000, min_order_notional=D("10"),
        max_gross_notional=D("500"), max_net_notional=D("500"),
        max_quote_deviation_bps=D("200"),
    ))
    client = FakeClient()
    adapter = HyperliquidMarketAdapter(
        HyperliquidVenueConfig.from_environment(dex="par", environ={})
    )
    gateway = SafeExecutionGateway(
        manager=manager, risk=risk, adapter=adapter, client=client,
        market_spec=HyperliquidMarketSpec(sz_decimals=4),
        live_enabled=live, operator_authorized=authorized,
    )
    return manager, risk, client, gateway


def new_bid(manager, price="99", size="0.2"):
    actions = manager.reconcile([
        OrderIntent("BTC", Side.BID, D(price), D(size))
    ])
    assert len(actions) == 1
    return actions[0]


def test_default_dry_run_never_contacts_sdk():
    manager, risk, client, gateway = setup()
    action = new_bid(manager)
    result = gateway.dispatch(action, context)
    assert result.allowed and not result.sent
    assert result.reason == "dry_run_validated_no_sdk"
    assert client.placements == []
    assert manager.live_orders[action.intent.client_order_id].state is OrderState.PENDING_NEW


def test_live_requires_operator_authorization():
    with pytest.raises(PermissionError):
        setup(live=True, authorized=False)


def test_mocked_live_place_ack_is_resting_and_has_correct_cloid():
    manager, risk, client, gateway = setup(live=True, authorized=True)
    action = new_bid(manager)
    result = gateway.dispatch(action, context)
    assert result.allowed and result.sent and result.reason == "resting"
    assert len(client.placements) == 1
    assert client.placements[0].client_order_id == action.intent.client_order_id
    assert manager.live_orders[action.intent.client_order_id].state is OrderState.RESTING


def test_adapter_quantization_mismatch_blocks_sdk_send():
    manager, risk, client, gateway = setup(live=True, authorized=True)
    action = new_bid(manager, price="99.9123")
    result = gateway.dispatch(action, context)
    assert not result.allowed and result.reason == "intent_must_be_venue_quantized"
    assert client.placements == []


def test_insufficient_capacity_is_blocked_after_adapter_conversion():
    manager, risk, client, gateway = setup(live=True, authorized=True)
    action = new_bid(manager)
    outcome = gateway.dispatch(action, lambda: context(capacity_buy={"BTC": D("0")}))
    assert outcome.reason == "insufficient_capacity"
    assert client.placements == []
    assert manager.live_orders[action.intent.client_order_id].state is OrderState.REJECTED


def test_stale_book_blocks_placement_without_reaching_sdk():
    manager, risk, client, gateway = setup(live=True, authorized=True)
    action = new_bid(manager)
    outcome = gateway.dispatch(action, lambda: context(now_ms=20000))
    assert outcome.reason == "stale_market_data"
    assert not client.placements


def test_cross_account_snapshot_blocked_even_if_other_fields_seem_safe():
    manager, risk, client, gateway = setup(live=True, authorized=True)
    action = new_bid(manager)
    outcome = gateway.dispatch(action, lambda: context(account_id="0x" + "b" * 40))
    assert outcome.reason == "gateway_account_or_dex_mismatch"
    assert not client.placements


def test_submit_timeout_quarantines_and_does_not_retry():
    manager, risk, client, gateway = setup(live=True, authorized=True)
    client.failure = TimeoutError("ambiguous")
    action = new_bid(manager)
    outcome = gateway.dispatch(action, context)
    assert outcome.reason == "submit_status_unknown"
    assert not manager.ready
    assert manager.live_orders[action.intent.client_order_id].state is OrderState.UNKNOWN
    assert manager.reconcile([OrderIntent("BTC", Side.BID, D("99"), D("0.2"))]) == []
    assert len(client.placements) == 1


def test_cancel_ack_not_terminal_and_cancel_allowed_during_risk_kill():
    manager, risk, client, gateway = setup(live=True, authorized=True)
    action = new_bid(manager)
    gateway.dispatch(action, context)
    risk.trigger_kill_switch()
    cancels = manager.request_cancel_all()
    assert len(cancels) == 1
    outcome = gateway.dispatch(cancels[0], context)
    assert outcome.reason == "cancel_accepted_not_terminal"
    assert client.cancels == [("BTC", action.intent.client_order_id)]
    assert manager.live_orders[action.intent.client_order_id].state is OrderState.PENDING_CANCEL
    assert manager.reconcile([]) == []


def test_cancel_timeout_does_not_mark_order_closed():
    manager, risk, client, gateway = setup(live=True, authorized=True)
    action = new_bid(manager)
    gateway.dispatch(action, context)
    cancel = manager.request_cancel_all()[0]
    client.failure = TimeoutError("ambiguous cancel")
    outcome = gateway.dispatch(cancel, context)
    assert outcome.reason == "cancel_status_unknown"
    assert manager.live_orders[action.intent.client_order_id].state is OrderState.UNKNOWN
    assert not manager.ready
    assert len(client.cancels) == 1


def test_cancel_dispatch_does_not_call_snapshot_provider():
    manager, risk, client, gateway = setup()
    action = new_bid(manager)
    manager.on_order_update(OrderEvent(
        action.intent.client_order_id, E.RESTING, 71,
        instrument="BTC", dex="par", account_id=ADDRESS,
    ))
    cancel = manager.request_cancel_all()[0]
    def unavailable():
        raise AssertionError("cancellations must not require fresh capacity")
    result = gateway.dispatch(cancel, unavailable)
    assert result.allowed and result.reason == "dry_run_cancel_no_sdk"
    assert not client.cancels


def test_unrecognized_action_cannot_bypass_gateway():
    manager, risk, client, gateway = setup()
    with pytest.raises(TypeError):
        gateway.dispatch(object(), context)
    assert not client.placements


def test_gateway_uses_manager_exposure_not_callers_empty_exposure_list():
    manager, risk, client, gateway = setup(live=True, authorized=True)
    ask = OrderIntent("BTC", Side.ASK, D("102"), D("2.1"))
    assert manager.reconcile([ask])  # A pending ask is still fillable.
    bids = manager.reconcile([ask, OrderIntent("BTC", Side.BID, D("99"), D("0.2"))])
    assert len(bids) == 1
    # Caller omitted exposure, but gateway must reconstruct it from manager.
    result = gateway.dispatch(bids[0], context)
    assert result.reason == "max_abs_position"
    assert not client.placements
