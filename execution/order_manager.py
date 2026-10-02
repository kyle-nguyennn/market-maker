from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class OrderState(str, Enum):
    PENDING_NEW = "PENDING_NEW"
    RESTING = "RESTING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class OrderIntent:
    instrument: str
    side: str
    price: Decimal
    size: Decimal
    post_only: bool = True
    reduce_only: bool = False
    client_order_id: str | None = None


@dataclass
class LiveOrder:
    client_order_id: str
    exchange_order_id: int | None
    instrument: str
    side: str
    price: Decimal
    original_size: Decimal
    remaining_size: Decimal
    state: OrderState


class OrderManager:
    """Reconcile desired intents against live/pending orders.

    Initial Perpetual Alpha policy:
    - one working order per side/instrument
    - prefer post-only (ALO)
    - suppress needless cancel/replace churn
    - reconcile partial fills and rejects explicitly
    """

    def __init__(self) -> None:
        self.live_orders: dict[str, LiveOrder] = {}

    def on_order_update(self, order: LiveOrder) -> None:
        self.live_orders[order.client_order_id] = order

    def reconcile(self, desired: list[OrderIntent]) -> list[object]:
        """Return venue-neutral actions needed to reach desired state."""
        _ = desired
        raise NotImplementedError("implement after read-only adapter is verified")
