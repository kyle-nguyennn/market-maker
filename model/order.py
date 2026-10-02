from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BID = "BID"
    ASK = "ASK"


@dataclass(frozen=True)
class Quote:
    """Desired strategy quote in venue-independent units."""

    price: Decimal
    qty: Decimal
    side: Side


@dataclass(frozen=True)
class OrderIntent:
    """Venue-independent execution intent.

    The adapter converts Decimal price/size and generic side semantics into the
    exact wire/SDK representation required by the target exchange.
    """

    instrument: str
    side: Side
    price: Decimal
    size: Decimal
    post_only: bool = True
    reduce_only: bool = False
    client_order_id: str | None = None
