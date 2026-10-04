from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from exchanges.sig_predictions.types import (
    OrderAction,
    OutcomeSide,
    SigBookLevel,
    SigMarketSpec,
    SigOrderBookSnapshot,
    SigOrderRequest,
    SigOrderResult,
)
from market_handler.market_adapter import BaseMarketAdapter
from model.market import BookLevel, MarketState
from model.order import ExecutionReport, ExecutionStatus, OrderIntent, Side


_PREFIX = "SIG_EXCHANGE"
_PRICE_TICK = Decimal("0.005")
_MIN_LIMIT_PRICE = Decimal("0.005")
_MAX_LIMIT_PRICE = Decimal("0.995")
_MAX_QUANTITY = 2_147_483_647


class SigPredictionsMarketAdapter(BaseMarketAdapter):
    """Translate SIG's YES-normalized exchange book into the shared model.

    One SIG exchange is one tradable contract. Internal prices are always YES
    probabilities. An internal BID is emitted as BUY YES at p; an internal ASK
    is emitted as BUY NO at 1-p. This mirrors the API's market-making workflow
    and avoids relying on server-side SELL canonicalization.
    """

    @staticmethod
    def internal_instrument(exchange_id: str) -> str:
        return f"{_PREFIX}:{exchange_id}"

    @staticmethod
    def _parse_exchange_id(value: str) -> str:
        prefix = f"{_PREFIX}:"
        exchange_id = value[len(prefix):] if value.startswith(prefix) else value
        if not exchange_id or not exchange_id.isdigit():
            raise ValueError("SIG exchange id must be a positive numeric string")
        if int(exchange_id) <= 0:
            raise ValueError("SIG exchange id must be positive")
        return exchange_id

    def to_internal_instrument(self, venue_instrument: str) -> str:
        return self.internal_instrument(self._parse_exchange_id(venue_instrument))

    def to_venue_instrument(self, instrument: str) -> str:
        return self._parse_exchange_id(instrument)

    @staticmethod
    def _levels(
        levels: Iterable[SigBookLevel],
        *,
        reverse: bool,
    ) -> tuple[BookLevel, ...]:
        converted = tuple(
            BookLevel(price=Decimal(level.price), size=Decimal(level.quantity))
            for level in levels
        )
        if any(level.price < 0 or level.price > 1 for level in converted):
            raise ValueError("prediction-market prices must be in [0, 1]")
        if any(level.size < 0 for level in converted):
            raise ValueError("book quantities must be non-negative")
        return tuple(sorted(converted, key=lambda x: x.price, reverse=reverse))

    def snapshot(self, snapshot: SigOrderBookSnapshot) -> MarketState:
        bids = self._levels(snapshot.bids, reverse=True)
        asks = self._levels(snapshot.asks, reverse=False)

        invalid_reason = None
        if not bids or not asks:
            invalid_reason = "missing_book_side"
        elif bids[0].price >= asks[0].price:
            invalid_reason = "crossed_book"

        return MarketState(
            instrument=self.internal_instrument(snapshot.exchange_id),
            timestamp_ms=snapshot.timestamp_ms,
            bids=bids,
            asks=asks,
            last_trade_price=(
                Decimal(snapshot.last_trade_price)
                if snapshot.last_trade_price is not None
                else None
            ),
            is_valid=invalid_reason is None,
            invalid_reason=invalid_reason,
        )

    @staticmethod
    def _validate_limit_price(price: Decimal) -> None:
        if price < _MIN_LIMIT_PRICE or price > _MAX_LIMIT_PRICE:
            raise ValueError("SIG limit price must be between 0.005 and 0.995")
        if price % _PRICE_TICK != 0:
            raise ValueError("SIG limit price must be on the 0.005 tick")

    @staticmethod
    def _quantity(size: Decimal) -> int:
        if size <= 0 or size != size.to_integral_value():
            raise ValueError("SIG quantity must be a positive integer number of shares")
        quantity = int(size)
        if quantity > _MAX_QUANTITY:
            raise ValueError("SIG quantity exceeds signed 32-bit engine limit")
        return quantity

    def to_venue_order(
        self,
        intent: OrderIntent,
        market_spec: SigMarketSpec,
    ) -> SigOrderRequest:
        exchange_id = self._parse_exchange_id(intent.instrument)
        if exchange_id != market_spec.exchange_id:
            raise ValueError("order intent instrument does not match SIG exchange")
        if intent.reduce_only:
            raise ValueError("reduce_only has no SIG /api/v1 order field")
        if not intent.client_order_id:
            raise ValueError(
                "SIG POST /orders requires client_order_id as idempotencyKey"
            )

        self._validate_limit_price(intent.price)
        quantity = self._quantity(intent.size)

        if intent.side is Side.BID:
            outcome = OutcomeSide.YES
            venue_price = intent.price
        else:
            outcome = OutcomeSide.NO
            venue_price = Decimal("1") - intent.price

        self._validate_limit_price(venue_price)

        # /api/v1 has no post-only flag. The caller may pre-check the current
        # book for maker intent, but the venue cannot guarantee post-only under
        # a race.
        return SigOrderRequest(
            exchange_id=exchange_id,
            outcome=outcome,
            action=OrderAction.BUY,
            quantity=quantity,
            price=float(venue_price),
            idempotency_key=intent.client_order_id,
            tournament_id=market_spec.tournament_id,
        )

    def to_internal_execution_report(
        self,
        venue_result: SigOrderResult,
    ) -> ExecutionReport:
        filled = Decimal(venue_result.quantity_traded)

        if venue_result.order_id is None and filled == 0:
            status = ExecutionStatus.NOOP
        elif venue_result.open and filled == 0:
            status = ExecutionStatus.RESTING
        elif venue_result.open:
            status = ExecutionStatus.PARTIALLY_FILLED
        elif venue_result.terminal_reason_code is not None:
            status = (
                ExecutionStatus.CANCELLED
                if filled == 0
                else ExecutionStatus.PARTIALLY_FILLED
            )
        elif (
            venue_result.quantity is not None
            and filled < Decimal(venue_result.quantity)
        ):
            status = (
                ExecutionStatus.CANCELLED
                if filled == 0
                else ExecutionStatus.PARTIALLY_FILLED
            )
        else:
            status = ExecutionStatus.FILLED

        return ExecutionReport(
            status=status,
            exchange_order_id=venue_result.order_id,
            filled_size=filled,
            # POST /orders returns fillPrice = last fill, not lifecycle average.
            # GET /orders/{id}/fills exposes exact avgFillPrice when required.
            average_price=None,
            error=venue_result.terminal_reason_code,
        )
