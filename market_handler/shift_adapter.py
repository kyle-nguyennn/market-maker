"""SHIFT ↔ venue-neutral Decimal conversion.

SHIFT expresses sizes in *lots of 100 shares*, including its order books.
The internal model always expresses sizes in shares. No SHIFT SDK import here.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from time import time_ns
from typing import Any, Iterable

from market_handler.market_adapter import BaseMarketAdapter
from model.market import BookLevel, MarketState
from model.order import (
    ExecutionReport, ExecutionStatus, OrderEvent, OrderEventType, OrderIntent, Side,
)

SHARES_PER_LOT = Decimal("100")


@dataclass(frozen=True)
class ShiftOrderRequest:
    symbol: str
    side: Side
    size_lots: int
    price: float


def _decimal(value: Any) -> Decimal:
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError("non-finite SHIFT numeric value")
    return d


def _status_name(status: Any) -> str:
    return str(getattr(status, "name", str(status))).split(".")[-1].upper()


class ShiftMarketAdapter(BaseMarketAdapter):
    """SDK-free converter; the client is solely responsible for SHIFT calls.

    The configured tick size must be verified against the competition VM.
    """

    def __init__(self, tick_size: Decimal = Decimal("0.01")) -> None:
        if not isinstance(tick_size, Decimal) or not tick_size.is_finite() or tick_size <= 0:
            raise ValueError("tick_size must be a positive finite Decimal")
        self.tick_size = tick_size

    def to_internal_instrument(self, venue_instrument: str) -> str:
        if not venue_instrument or ":" in venue_instrument:
            raise ValueError("SHIFT expects an unqualified ticker")
        return venue_instrument

    def to_venue_instrument(self, instrument: str) -> str:
        return self.to_internal_instrument(instrument)

    @staticmethod
    def _levels(entries: Iterable[Any], *, reverse: bool) -> tuple[BookLevel, ...]:
        aggregated: dict[Decimal, Decimal] = {}
        for entry in entries:
            price = _decimal(entry.price)
            size = _decimal(entry.size) * SHARES_PER_LOT
            if price <= 0 or size <= 0:
                raise ValueError("invalid SHIFT book price or size")
            aggregated[price] = aggregated.get(price, Decimal(0)) + size
        return tuple(
            BookLevel(price, aggregated[price])
            for price in sorted(aggregated, reverse=reverse)
        )

    def snapshot(
        self, symbol: str, bids: Iterable[Any], asks: Iterable[Any],
        *, timestamp_ms: int | None = None, last_price: Any | None = None,
    ) -> MarketState:
        bid_levels = self._levels(bids, reverse=True)
        ask_levels = self._levels(asks, reverse=False)
        reason = None
        if not bid_levels or not ask_levels:
            reason = "missing_book_side"
        elif bid_levels[0].price >= ask_levels[0].price:
            reason = "crossed_book"
        return MarketState(
            instrument=self.to_internal_instrument(symbol),
            timestamp_ms=time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms,
            bids=bid_levels, asks=ask_levels,
            last_trade_price=_decimal(last_price) if last_price is not None and last_price > 0 else None,
            is_valid=reason is None, invalid_reason=reason,
        )

    def to_venue_order(self, intent: OrderIntent, market_spec: Any = None) -> ShiftOrderRequest:
        if intent.reduce_only:
            raise ValueError("SHIFT has no documented reduce-only order")
        if not isinstance(intent.side, Side):
            raise ValueError("invalid side")
        if not isinstance(intent.price, Decimal) or not intent.price.is_finite() or intent.price <= 0:
            raise ValueError("invalid price")
        if not isinstance(intent.size, Decimal) or not intent.size.is_finite() or intent.size <= 0:
            raise ValueError("invalid share quantity")
        lots = intent.size / SHARES_PER_LOT
        if lots != lots.to_integral_value():
            raise ValueError("SHIFT size must be an integer multiple of 100 shares")
        rounding = ROUND_DOWN if intent.side is Side.BID else ROUND_UP
        ticks = (intent.price / self.tick_size).to_integral_value(rounding=rounding)
        rounded = ticks * self.tick_size
        if rounded <= 0:
            raise ValueError("price becomes nonpositive after tick rounding")
        return ShiftOrderRequest(
            symbol=self.to_venue_instrument(intent.instrument), side=intent.side,
            size_lots=int(lots), price=float(rounded),
        )

    def is_non_marketable(self, request: ShiftOrderRequest, state: MarketState) -> bool:
        """Best-effort check ONLY; SHIFT does not expose post-only / ALO."""
        if not state.is_valid or state.instrument != request.symbol:
            return False
        price = _decimal(request.price)
        if request.side is Side.BID:
            return state.best_ask is not None and price < state.best_ask
        return state.best_bid is not None and price > state.best_bid

    def to_internal_execution_report(self, venue_result: Any) -> ExecutionReport:
        status = _status_name(venue_result.status)
        status_map = {
            "NEW": ExecutionStatus.RESTING,
            "PARTIALLY_FILLED": ExecutionStatus.RESTING,
            "FILLED": ExecutionStatus.FILLED,
            "REJECTED": ExecutionStatus.REJECTED,
        }
        if status not in status_map:
            raise ValueError(f"SHIFT status {status} requires further reconciliation")
        executed = _decimal(venue_result.executed_size) * SHARES_PER_LOT
        execution_price = _decimal(venue_result.executed_price) if executed else None
        return ExecutionReport(
            status=status_map[status],
            exchange_order_id=str(venue_result.id),
            filled_size=executed, average_price=execution_price,
        )

    def to_internal_order_event(self, client_order_id: str, venue_order: Any) -> OrderEvent:
        """Absolute executed_size from an order snapshot is CUMULATIVE."""
        status = _status_name(venue_order.status)
        event_types = {
            "NEW": OrderEventType.RESTING,
            "PARTIALLY_FILLED": OrderEventType.PARTIAL_FILL,
            "FILLED": OrderEventType.FILLED,
            "PENDING_CANCEL": OrderEventType.CANCEL_ACCEPTED,
            "CANCELED": OrderEventType.CANCELLED,
            "REJECTED": OrderEventType.REJECTED,
            "PENDING_NEW": OrderEventType.UNKNOWN,
        }
        if status not in event_types:
            raise ValueError(f"unrecognized SHIFT status: {status}")
        return OrderEvent(
            client_order_id=client_order_id,
            kind=event_types[status],
            exchange_order_id=str(venue_order.id),
            cumulative_filled_size=_decimal(venue_order.executed_size) * SHARES_PER_LOT,
            instrument=self.to_internal_instrument(venue_order.symbol),
            reason="pending_new_unconfirmed" if status == "PENDING_NEW" else None,
        )
