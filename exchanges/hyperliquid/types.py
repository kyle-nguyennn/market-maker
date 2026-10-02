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
class VenueOrderRequest:
    instrument: str
    side: OrderSide
    size: Decimal
    price: Decimal
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
