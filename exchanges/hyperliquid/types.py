from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TimeInForce(str, Enum):
    GTC = "Gtc"
    IOC = "Ioc"
    ALO = "Alo"


@dataclass(frozen=True)
class HyperliquidMarketSpec:
    """Exchange precision metadata read from Hyperliquid meta."""

    sz_decimals: int


@dataclass(frozen=True)
class VenueOrderRequest:
    """Hyperliquid-facing order representation.

    Strings intentionally mirror the decimal-string representation expected at
    the exchange boundary. Internal strategy/execution objects remain Decimal.
    """

    instrument: str
    side: OrderSide
    size: str
    price: str
    tif: TimeInForce = TimeInForce.ALO
    reduce_only: bool = False
    client_order_id: str | None = None


@dataclass(frozen=True)
class VenueOrderResult:
    """Normalized result for one operation inside a Hyperliquid response."""

    status: str
    order_id: int | None = None
    filled_size: Decimal | None = None
    average_price: Decimal | None = None
    error: str | None = None
