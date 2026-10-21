from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BID = "BID"
    ASK = "ASK"


class ExecutionStatus(str, Enum):
    RESTING = "RESTING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    NOOP = "NOOP"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Quote:
    """Desired strategy quote in venue-independent units."""

    price: Decimal
    qty: Decimal
    side: Side


@dataclass(frozen=True)
class OrderIntent:
    """Venue-independent intent. Adapter owns all precision and serialization."""

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
    exchange_order_id: str | int | None = None
    filled_size: Decimal | None = None
    average_price: Decimal | None = None
    error: str | None = None


class OrderEventType(str, Enum):
    """Normalized venue event, NOT an unverified SDK message shape.

    A successful cancel request is CANCEL_ACCEPTED, not CANCELLED.
    A fill event supplies total CUMULATIVE filled size, not the last trade size.
    UNKNOWN covers ambiguous send/cancel timeouts and requires recovery.
    """

    RESTING = "RESTING"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCEL_ACCEPTED = "CANCEL_ACCEPTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OrderEvent:
    client_order_id: str
    kind: OrderEventType
    exchange_order_id: int | None = None
    cumulative_filled_size: Decimal | None = None
    instrument: str | None = None
    dex: str | None = None
    account_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PlaceAction:
    """Proposed new order. Never executes an SDK request itself."""

    intent: OrderIntent


@dataclass(frozen=True)
class CancelAction:
    """Request cancellation; an accepted request is not terminal proof."""

    instrument: str
    client_order_id: str
    exchange_order_id: int | None = None


OrderAction = PlaceAction | CancelAction
