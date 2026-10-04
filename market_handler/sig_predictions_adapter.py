from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from exchanges.sig_predictions.types import (
    Outcome,
    SigBookLevel,
    SigMarketSpec,
    SigOrderBookSnapshot,
    SigOrderRequest,
    SigOrderResult,
    VenueOrderType,
    VenueSide,
)
from market_handler.market_adapter import BaseMarketAdapter
from model.market import BookLevel, MarketState
from model.order import ExecutionReport, ExecutionStatus, OrderIntent, Side


_PREFIX = "SIG_PREDICTION"


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class SigPredictionsMarketAdapter(BaseMarketAdapter):
    """Map SIG prediction-market semantics to the shared Decimal model."""

    @staticmethod
    def internal_instrument(market_id: str, outcome: Outcome) -> str:
        return f"{_PREFIX}:{market_id}:{outcome.value}"

    @staticmethod
    def _parse_compound_instrument(value: str) -> tuple[str, Outcome]:
        parts = value.split(":")
        if len(parts) == 3 and parts[0] == _PREFIX:
            market_id, outcome = parts[1], parts[2]
        elif len(parts) == 2:
            market_id, outcome = parts
        else:
            raise ValueError(
                "SIG instrument must be SIG_PREDICTION:<market_id>:<YES|NO> "
                "or <market_id>:<YES|NO>"
            )
        if not market_id:
            raise ValueError("market_id must be non-empty")
        return market_id, Outcome(outcome.upper())

    def to_internal_instrument(self, venue_instrument: str) -> str:
        market_id, outcome = self._parse_compound_instrument(venue_instrument)
        return self.internal_instrument(market_id, outcome)

    def to_venue_instrument(self, instrument: str) -> str:
        market_id, outcome = self._parse_compound_instrument(instrument)
        # This is a venue-specific compound token used at the adapter/client
        # boundary, not an assertion about a single wire-level API field.
        return f"{market_id}:{outcome.value}"

    @staticmethod
    def _levels(
        levels: Iterable[SigBookLevel],
        *,
        reverse: bool,
    ) -> tuple[BookLevel, ...]:
        converted = tuple(
            BookLevel(price=Decimal(level.price), size=Decimal(level.size))
            for level in levels
        )
        if any(level.price < 0 or level.price > 1 for level in converted):
            raise ValueError("prediction-market prices must be in [0, 1]")
        if any(level.size < 0 for level in converted):
            raise ValueError("book sizes must be non-negative")
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
            instrument=self.internal_instrument(snapshot.market_id, snapshot.outcome),
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

    def to_venue_order(
        self,
        intent: OrderIntent,
        market_spec: SigMarketSpec,
    ) -> SigOrderRequest:
        market_id, outcome = self._parse_compound_instrument(intent.instrument)
        if market_id != market_spec.market_id or outcome is not market_spec.outcome:
            raise ValueError("order intent instrument does not match SIG market spec")
        if intent.price < 0 or intent.price > 1:
            raise ValueError("prediction-market limit price must be in [0, 1]")
        if intent.size <= 0:
            raise ValueError("order size must be positive")
        if intent.reduce_only:
            raise ValueError("reduce_only has no documented SIG Predictions meaning")

        return SigOrderRequest(
            market_id=market_id,
            outcome=outcome,
            side=VenueSide.BUY if intent.side is Side.BID else VenueSide.SELL,
            quantity=_decimal_text(intent.size),
            order_type=VenueOrderType.LIMIT,
            limit_price=_decimal_text(intent.price),
            client_order_id=intent.client_order_id,
            post_only_requested=intent.post_only,
        )

    def to_internal_execution_report(
        self,
        venue_result: SigOrderResult,
    ) -> ExecutionReport:
        normalized = venue_result.status.strip().lower()
        if normalized in {"resting", "open", "accepted"}:
            status = ExecutionStatus.RESTING
        elif normalized in {"filled", "complete", "completed"}:
            status = ExecutionStatus.FILLED
        elif normalized in {"rejected", "error", "failed"}:
            status = ExecutionStatus.REJECTED
        else:
            raise ValueError(f"unknown SIG order status: {venue_result.status}")

        return ExecutionReport(
            status=status,
            exchange_order_id=venue_result.order_id,
            filled_size=(
                Decimal(venue_result.filled_quantity)
                if venue_result.filled_quantity is not None
                else None
            ),
            average_price=(
                Decimal(venue_result.average_price)
                if venue_result.average_price is not None
                else None
            ),
            error=venue_result.error,
        )
