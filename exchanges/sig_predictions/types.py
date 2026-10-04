from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Outcome(str, Enum):
    YES = "YES"
    NO = "NO"


class VenueSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class VenueOrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


@dataclass(frozen=True)
class SigBookLevel:
    price: str
    size: str


@dataclass(frozen=True)
class SigOrderBookSnapshot:
    """Client-normalized SIG book before Decimal conversion."""

    market_id: str
    outcome: Outcome
    timestamp_ms: int
    bids: tuple[SigBookLevel, ...]
    asks: tuple[SigBookLevel, ...]
    as_of: str | int | None = None
    last_trade_price: str | None = None


@dataclass(frozen=True)
class SigOrderRequest:
    """Venue command emitted by the adapter.

    Exact HTTP field names belong in the client once copied from the public
    API Reference. This object preserves documented market semantics without
    guessing the wire schema.
    """

    market_id: str
    outcome: Outcome
    side: VenueSide
    quantity: str
    order_type: VenueOrderType
    limit_price: str | None = None
    client_order_id: str | None = None
    post_only_requested: bool = False


@dataclass(frozen=True)
class SigOrderResult:
    status: str
    order_id: str | int | None = None
    filled_quantity: str | None = None
    average_price: str | None = None
    error: str | None = None
