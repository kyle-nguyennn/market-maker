from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Iterable

from exchanges.hyperliquid.config import HyperliquidVenueConfig
from exchanges.hyperliquid.types import (
    HyperliquidL2Snapshot,
    HyperliquidMarketSpec,
    parse_l2_snapshot,
    OrderSide,
    TimeInForce,
    VenueOrderRequest,
    VenueOrderResult,
)
from market_handler.market_adapter import BaseMarketAdapter
from model.market import BookLevel, MarketState
from model.order import ExecutionReport, ExecutionStatus, OrderIntent, Side


def _strip_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class HyperliquidMarketAdapter(BaseMarketAdapter):
    """Translate Hyperliquid venue data into the internal Decimal model.

    This class is the only layer allowed to apply Hyperliquid symbol,
    precision, and serialization rules.
    """

    def __init__(self, config: HyperliquidVenueConfig | None = None) -> None:
        self.config = config or HyperliquidVenueConfig.from_environment()

    def to_internal_instrument(self, venue_instrument: str) -> str:
        prefix = f"{self.config.dex}:"
        if venue_instrument.startswith(prefix):
            return venue_instrument[len(prefix):]
        if ":" in venue_instrument:
            raise ValueError(f"instrument is not on dex {self.config.dex}: {venue_instrument}")
        return venue_instrument

    def to_venue_instrument(self, instrument: str) -> str:
        return self.config.venue_symbol(instrument)

    @staticmethod
    def _levels(rows: Iterable[tuple[str, str]]) -> tuple[BookLevel, ...]:
        levels = tuple(BookLevel(Decimal(px), Decimal(sz)) for px, sz in rows)
        if any(not level.price.is_finite() or not level.size.is_finite()
               or level.price <= 0 or level.size <= 0 for level in levels):
            raise ValueError("L2 prices and sizes must be finite and positive")
        return levels

    def snapshot(
        self,
        *,
        instrument: str,
        timestamp_ms: int,
        bids: Iterable[tuple[str, str]],
        asks: Iterable[tuple[str, str]],
        last_trade_price: str | None = None,
        oracle_price: str | None = None,
        mark_price: str | None = None,
        funding_rate: str | None = None,
        open_interest: str | None = None,
    ) -> MarketState:
        bid_levels = self._levels(bids)
        ask_levels = self._levels(asks)

        invalid_reason = None
        if not bid_levels or not ask_levels:
            invalid_reason = "missing_book_side"
        elif bid_levels[0].price >= ask_levels[0].price:
            invalid_reason = "crossed_book"
        elif any(left.price <= right.price for left, right in zip(bid_levels, bid_levels[1:])) or \
             any(left.price >= right.price for left, right in zip(ask_levels, ask_levels[1:])):
            invalid_reason = "unsorted_book"

        return MarketState(
            instrument=self.to_internal_instrument(instrument),
            timestamp_ms=timestamp_ms,
            bids=bid_levels,
            asks=ask_levels,
            last_trade_price=Decimal(last_trade_price) if last_trade_price else None,
            oracle_price=Decimal(oracle_price) if oracle_price else None,
            mark_price=Decimal(mark_price) if mark_price else None,
            funding_rate=Decimal(funding_rate) if funding_rate else None,
            open_interest=Decimal(open_interest) if open_interest else None,
            is_valid=invalid_reason is None,
            invalid_reason=invalid_reason,
        )

    def market_spec_from_meta(self, instrument: str, meta: dict) -> HyperliquidMarketSpec:
        """Read lot precision from the live par metadata, never from constants."""
        symbol = self.to_venue_instrument(instrument)
        for row in meta["universe"]:
            if row["name"] == symbol:
                return HyperliquidMarketSpec(sz_decimals=int(row["szDecimals"]))
        raise ValueError(f"market not found in par metadata: {symbol}")

    def from_sdk_l2_snapshot(self, book: HyperliquidL2Snapshot, *,
                             asset_ctx: dict | None = None) -> MarketState:
        """Convert SDK L2 + optional mark/oracle context to the internal model.

        The SDK's wire fields are decimal strings. Conversion to Decimal belongs
        exclusively to this adapter; the client never invents prices or ticks.
        """
        book = parse_l2_snapshot(book)
        levels = book["levels"]
        context = asset_ctx or {}
        return self.snapshot(
            instrument=book["coin"],
            timestamp_ms=int(book["time"]),
            bids=((level["px"], level["sz"]) for level in levels[0]),
            asks=((level["px"], level["sz"]) for level in levels[1]),
            oracle_price=context.get("oraclePx"),
            mark_price=context.get("markPx"),
            funding_rate=context.get("funding"),
            open_interest=context.get("openInterest"),
        )

    @staticmethod
    def _quantize_size(size: Decimal, sz_decimals: int) -> Decimal:
        quantum = Decimal(1).scaleb(-sz_decimals)
        quantized = size.quantize(quantum, rounding=ROUND_DOWN)
        if quantized <= 0:
            raise ValueError("order size becomes zero after venue quantization")
        return quantized

    @staticmethod
    def _quantize_price(price: Decimal, side: Side, sz_decimals: int) -> Decimal:
        if price <= 0:
            raise ValueError("price must be positive")

        # Hyperliquid allows integer prices. For non-integers, enforce both
        # constraints from the participant guide:
        #   - <= 5 significant figures
        #   - <= 6 - szDecimals decimal places
        if price == price.to_integral_value():
            return price

        max_decimals = 6 - sz_decimals
        decimal_exponent = -max_decimals
        significant_exponent = price.adjusted() - 4
        quantum_exponent = max(decimal_exponent, significant_exponent)
        quantum = Decimal(1).scaleb(quantum_exponent)

        rounding = ROUND_DOWN if side is Side.BID else ROUND_UP
        return price.quantize(quantum, rounding=rounding)

    def to_venue_order(
        self,
        intent: OrderIntent,
        market_spec: HyperliquidMarketSpec,
    ) -> VenueOrderRequest:
        price = self._quantize_price(intent.price, intent.side, market_spec.sz_decimals)
        size = self._quantize_size(intent.size, market_spec.sz_decimals)

        venue_side = OrderSide.BUY if intent.side is Side.BID else OrderSide.SELL
        tif = TimeInForce.ALO if intent.post_only else TimeInForce.GTC

        return VenueOrderRequest(
            instrument=self.to_venue_instrument(intent.instrument),
            side=venue_side,
            size=_strip_decimal(size),
            price=_strip_decimal(price),
            tif=tif,
            reduce_only=intent.reduce_only,
            client_order_id=intent.client_order_id,
        )

    def to_internal_execution_report(
        self,
        venue_result: VenueOrderResult,
    ) -> ExecutionReport:
        status_map = {
            "resting": ExecutionStatus.RESTING,
            "filled": ExecutionStatus.FILLED,
            "error": ExecutionStatus.REJECTED,
        }
        try:
            status = status_map[venue_result.status.lower()]
        except KeyError as exc:
            raise ValueError(f"unknown Hyperliquid order status: {venue_result.status}") from exc

        return ExecutionReport(
            status=status,
            exchange_order_id=venue_result.order_id,
            filled_size=(
                Decimal(venue_result.filled_size)
                if venue_result.filled_size is not None
                else None
            ),
            average_price=(
                Decimal(venue_result.average_price)
                if venue_result.average_price is not None
                else None
            ),
            error=venue_result.error,
        )
