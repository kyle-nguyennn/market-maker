from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BID = "BID"
    ASK = "ASK"


class ExecutionStatus(str, Enum):
    RESTING = "RESTING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Quote:
    """Desired strategy quote in venue-independent units."""

    price: Decimal
    qty: Decimal
    side: Side


@dataclass(frozen=True)
class OrderIntent:
    """Venue-independent execution intent.

    Numeric fields remain Decimal until a market adapter converts them to the
    target venue's exact wire/SDK representation.
    """

    instrument: str
    side: Side
    price: Decimal
    size: Decimal
    post_only: bool = True
    reduce_only: bool = False
    client_order_id: str | None = None


@dataclass(frozen=True)
class ExecutionReport:
    """Venue-independent result of one order operation."""

    status: ExecutionStatus
    exchange_order_id: int | None = None
    filled_size: Decimal | None = None
    average_price: Decimal | None = None
    error: str | None = None
