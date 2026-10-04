from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OutcomeSide(str, Enum):
    YES = "yes"
    NO = "no"


class OrderAction(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class SigMarketSpec:
    """Resolved trading context for one atomic tradable exchange."""

    market_id: str
    exchange_id: str
    option: str | None = None
    tournament_id: str | None = None


@dataclass(frozen=True)
class SigBookVersion:
    sequence: int
    at: str


@dataclass(frozen=True)
class SigBookLevel:
    price: str
    quantity: str


@dataclass(frozen=True)
class SigOrderBookSnapshot:
    """Client-normalized YES-denominated exchange order book."""

    exchange_id: str
    market_id: str
    timestamp_ms: int
    bids: tuple[SigBookLevel, ...]
    asks: tuple[SigBookLevel, ...]
    as_of: SigBookVersion | None = None
    last_trade_price: str | None = None


@dataclass(frozen=True)
class SigOrderRequest:
    """Exact participant order semantics before JSON serialization."""

    exchange_id: str
    outcome: OutcomeSide
    action: OrderAction
    quantity: int
    price: float | None
    idempotency_key: str | None = None
    tournament_id: str | None = None
    expiration_date: str | None = None


@dataclass(frozen=True)
class SigOrderResult:
    """Normalized POST /orders response.

    fill_price is the response's last-fill field, not an average fill price.
    """

    order_id: int | None
    exchange_id: str
    open: bool
    quantity_traded: str
    total_cost: str
    fill_price: str | None
    remaining_quantity: str | None = None
    action: OrderAction | None = None
    outcome: OutcomeSide | None = None
    price: str | None = None
    quantity: int | None = None
    terminal_reason_code: str | None = None
