from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from model.order import OrderIntent, Side


class OrderState(str, Enum):
    PENDING_NEW = "PENDING_NEW"
    RESTING = "RESTING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class LiveOrder:
    client_order_id: str
    exchange_order_id: int | None
    instrument: str
    side: Side
    price: Decimal
    original_size: Decimal
    remaining_size: Decimal
    state: OrderState


class OrderManager:
    """Reconcile venue-independent desired intents against live orders.

    All numeric fields remain Decimal here. Quantization and serialization are
    the responsibility of the selected market adapter at the final boundary.
    """

    def __init__(self) -> None:
        self.live_orders: dict[str, LiveOrder] = {}

    def on_order_update(self, order: LiveOrder) -> None:
        self.live_orders[order.client_order_id] = order

    def reconcile(self, desired: list[OrderIntent]) -> list[object]:
        """Return venue-neutral actions needed to reach desired state."""
        _ = desired
        raise NotImplementedError("implement after read-only adapter is verified")
