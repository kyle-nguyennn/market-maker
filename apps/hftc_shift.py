"""HFTC SHIFT bridge: read-only default; explicit, capped demo trading opt-in.

Run inside the SHIFT-equipped competition VM, not against an invented endpoint.
No historical-date oracle, no forced 200 trades, no blind order retries.
"""
from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from decimal import Decimal

from exchanges.shift.client import ShiftVenueClient
from market_handler.shift_adapter import ShiftMarketAdapter, _status_name
from model.market import MarketState
from model.order import OrderIntent, Quote, Side
from quoter.base_quoter import BaseQuoter


@dataclass(frozen=True)
class HftcLimits:
    order_shares: Decimal = Decimal("100")
    max_abs_position_shares: Decimal = Decimal("1000")
    max_order_notional: Decimal = Decimal("25000")
    max_bid_commitment: Decimal = Decimal("50000")
    tick_size: Decimal = Decimal("0.01")
    inventory_skew_per_lot: Decimal = Decimal("0.01")


class HftcQuoter(BaseQuoter):
    """Portable Decimal strategy: top-of-book quoting with inventory skew."""

    def __init__(self, limits: HftcLimits) -> None:
        self.limits = limits
        self.inventory = Decimal(0)

    def set_inventory(self, position_shares: Decimal) -> None:
        self.inventory = position_shares

    def quote(self, state: MarketState) -> list[Quote]:
        if not state.is_valid or state.best_bid is None or state.best_ask is None:
            return []
        spread = state.best_ask - state.best_bid
        if spread <= 0:
            return []
        mid = (state.best_bid + state.best_ask) / Decimal(2)
        skew = self.inventory / Decimal(100) * self.limits.inventory_skew_per_lot
        half = max(spread / Decimal(2), self.limits.tick_size / Decimal(2))
        bid = mid - half - skew
        ask = mid + half - skew
        qty = self.limits.order_shares
        if qty <= 0:
            return []
        return [Quote(bid, qty, Side.BID), Quote(ask, qty, Side.ASK)]


def _requests(
    state: MarketState, position: Decimal, buying_power: Decimal,
    adapter: ShiftMarketAdapter, quoter: HftcQuoter, limits: HftcLimits,
):
    quoter.set_inventory(position)
    orders = []
    for quote in quoter.quote(state):
        intent = OrderIntent(
            instrument=state.instrument, side=quote.side,
            size=quote.qty, price=quote.price, post_only=True,
        )
        req = adapter.to_venue_order(intent)
        shares = Decimal(req.size_lots) * Decimal(100)
        notional = Decimal(str(req.price)) * shares
        if not adapter.is_non_marketable(req, state):
            continue
        if shares > limits.max_abs_position_shares or notional > limits.max_order_notional:
            continue
        if req.side is Side.BID:
            if position + shares > limits.max_abs_position_shares:
                continue
            if notional > min(buying_power, limits.max_bid_commitment):
                continue
        else:
            if position - shares < -limits.max_abs_position_shares:
                continue
        orders.append(req)
    return orders


def _same_quotes(existing, requests) -> bool:
    if len(existing) != len(requests):
        return False
    wanted = sorted(
        (r.side.value, r.size_lots, Decimal(str(r.price))) for r in requests
    )
    try:
        actual = sorted(
            (
                ("BID" if _status_name(o.type) == "LIMIT_BUY" else "ASK"),
                int(o.size), Decimal(str(o.price)),
            )
            for o in existing
        )
    except (ValueError, TypeError):
        return False
    return actual == wanted


def _await_order(client: ShiftVenueClient, order_id: str, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            status = _status_name(client.trader.get_order(order_id).status)
        except (KeyError, LookupError):
            status = "PENDING_NEW"
        if status in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
            return
        if status in {"REJECTED", "CANCELED"}:
            raise RuntimeError(f"SHIFT order {order_id} terminal status: {status}")
        time.sleep(0.1)
    raise RuntimeError(f"Unconfirmed SHIFT order {order_id}; no retry until reconciled")


def _await_no_pending(client: ShiftVenueClient, symbol: str, timeout: float = 8.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not client.pending_orders(symbol):
            return
        time.sleep(0.1)
    raise RuntimeError("SHIFT cancellation unconfirmed; new quotes blocked")


def _cancel(client: ShiftVenueClient, symbol: str) -> None:
    if client.pending_orders(symbol):
        client.cancel_symbol(symbol)
        _await_no_pending(client, symbol)


def _flatten(client: ShiftVenueClient, symbol: str) -> None:
    """One safe close attempt; never resend while the first one is unresolved."""
    _cancel(client, symbol)
    shares = client.position_shares(symbol)
    if shares == 0:
        return
    order_id = client.submit_market_close(symbol, shares)
    if order_id is None:
        return
    _await_order(client, order_id)
    # No second market order: a partial fill may still be in flight.
    remaining = client.position_shares(symbol)
    if remaining:
        raise RuntimeError(
            f"Unflattened {symbol} position: {remaining} shares; manual reconciliation needed"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Stevens SHIFT market-maker (read-only by default)")
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--depth", type=int, default=10)
    parser.add_argument("--trade", action="store_true", help="enable actual orders with env safeguard")
    parser.add_argument("--duration-seconds", type=int, default=300)
    parser.add_argument("--flatten-buffer-seconds", type=int, default=30)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--order-shares", default="100")
    parser.add_argument("--max-position-shares", default="1000")
    parser.add_argument("--max-order-notional", default="25000")
    parser.add_argument("--tick-size", default="0.01")
    args = parser.parse_args()

    if args.trade and os.environ.get("SHIFT_ALLOW_TRADING") != "yes":
        parser.error("--trade requires SHIFT_ALLOW_TRADING=yes")
    if args.duration_seconds <= args.flatten_buffer_seconds or args.poll_seconds <= 0:
        parser.error("duration must exceed flatten buffer; poll interval must be positive")
    limits = HftcLimits(
        order_shares=Decimal(args.order_shares),
        max_abs_position_shares=Decimal(args.max_position_shares),
        max_order_notional=Decimal(args.max_order_notional),
        tick_size=Decimal(args.tick_size),
    )
    for quantity in (limits.order_shares, limits.max_abs_position_shares):
        if quantity <= 0 or quantity % Decimal(100):
            parser.error("SHIFT order/position quantities must be positive multiples of 100 shares")
    if limits.max_order_notional <= 0:
        parser.error("max-order-notional must be positive")
    username, password = os.environ.get("SHIFT_USERNAME"), os.environ.get("SHIFT_PASSWORD")
    cfg = os.environ.get("SHIFT_INITIATOR_CFG", "initiator.cfg")
    if not username or not password:
        parser.error("set SHIFT_USERNAME and SHIFT_PASSWORD in the SHIFT VM")
    adapter = ShiftMarketAdapter(tick_size=limits.tick_size)
    client = ShiftVenueClient.connect(
        username=username, password=password, initiator_cfg=cfg, adapter=adapter,
    )
    try:
        if args.symbol not in client.symbols():
            raise RuntimeError(f"SHIFT symbol unavailable: {args.symbol}")
        state = client.snapshot(args.symbol, depth=args.depth)
        position = client.position_shares(args.symbol)
        bp = client.buying_power()
        print(f"{args.symbol} book={state.best_bid}/{state.best_ask}, "
              f"position_shares={position}, buying_power={bp}, valid={state.is_valid}")
        if not args.trade:
            print("READ-ONLY: no orders placed or cancelled.")
            return

        quoter = HftcQuoter(limits)
        deadline = time.monotonic() + args.duration_seconds
        try:
            while time.monotonic() < deadline - args.flatten_buffer_seconds:
                state = client.snapshot(args.symbol, depth=args.depth)
                if not state.is_valid:
                    raise RuntimeError(f"Invalid SHIFT order book: {state.invalid_reason}")
                position = client.position_shares(args.symbol)
                bp = client.buying_power()
                desired = _requests(state, position, bp, adapter, quoter, limits)
                existing = client.pending_orders(args.symbol)
                if not _same_quotes(existing, desired):
                    _cancel(client, args.symbol)
                    # Book and risk can change during cancellation; re-evaluate.
                    state = client.snapshot(args.symbol, depth=args.depth)
                    if not state.is_valid:
                        raise RuntimeError("Invalid book after quote cancellation")
                    desired = _requests(
                        state, client.position_shares(args.symbol),
                        client.buying_power(), adapter, quoter, limits,
                    )
                    for request in desired:
                        fresh = client.snapshot(args.symbol, depth=args.depth)
                        if not adapter.is_non_marketable(request, fresh):
                            continue
                        order_id = client.submit_limit(request, latest=fresh)
                        _await_order(client, order_id)
                time.sleep(args.poll_seconds)
        finally:
            # Trading is intraday; attempt cancellation/flatten even after strategy error.
            _flatten(client, args.symbol)
            print(f"Executed fills observed: {client.execution_count(args.symbol)} "
                  "(historical session count, not a guarantee of 200 daily fills)")
    finally:
        client.close()


if __name__ == "__main__":
    main()
