from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from uuid import NAMESPACE_URL, uuid4, uuid5

from model.order import (
    CancelAction, OrderAction, OrderEvent, OrderEventType, OrderIntent,
    PlaceAction, Side,
)


class OrderState(str, Enum):
    PENDING_NEW = "PENDING_NEW"
    RESTING = "RESTING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATES = frozenset({
    OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED,
})


class ReconciliationError(RuntimeError):
    """A venue snapshot/event cannot safely be reconciled with local state."""


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
    post_only: bool = True
    reduce_only: bool = False

    @property
    def filled_size(self) -> Decimal:
        return self.original_size - self.remaining_size

    @property
    def active(self) -> bool:
        return self.state not in TERMINAL_STATES


class OrderManager:
    """Venue-independent, fail-closed cancel/wait/replace state machine.

    reconcile() atomically *reserves* planned requests by changing local state,
    but never calls a venue. The caller must dispatch actions serially and send
    normalized outcomes through on_order_update(). A send timeout is UNKNOWN,
    NOT REJECTED; query order status and resynchronize before new placements.

    Pass *adapter-quantized* OrderIntent objects to reconcile() to avoid churn.
    A unique session_id and the generated 128-bit CLOIDs identify submissions;
    caller must persist actions/state before attempting network dispatch.
    """

    def __init__(
        self, *, session_id: str | None = None,
        dex: str | None = None, account_id: str | None = None,
    ) -> None:
        self.live_orders: dict[str, LiveOrder] = {}
        self._session_id = session_id or uuid4().hex
        self._sequence = 0
        self.dex = dex
        self.account_id = account_id
        self.ready = False
        self.blocked_reason: str | None = "startup_sync_required"

    def start_recovery(self, reason: str = "reconnect") -> None:
        self.ready = False
        self.blocked_reason = reason

    @staticmethod
    def _validate_intent(intent: OrderIntent) -> None:
        if not intent.instrument or ":" in intent.instrument:
            raise ValueError("instrument must be a logical, nonempty symbol")
        if not isinstance(intent.side, Side):
            raise ValueError("invalid order side")
        if not isinstance(intent.price, Decimal) or not intent.price.is_finite() or intent.price <= 0:
            raise ValueError("order price must be positive finite Decimal")
        if not isinstance(intent.size, Decimal) or not intent.size.is_finite() or intent.size <= 0:
            raise ValueError("order size must be positive finite Decimal")
        if intent.client_order_id is not None:
            raise ValueError("manager owns CLOID allocation; desired intents must omit it")

    @staticmethod
    def _validate_order(order: LiveOrder) -> None:
        if not order.instrument or ":" in order.instrument or not isinstance(order.side, Side):
            raise ReconciliationError("invalid recovered order identity")
        for field in (order.price, order.original_size, order.remaining_size):
            if not isinstance(field, Decimal) or not field.is_finite():
                raise ReconciliationError("invalid Decimal in recovered order")
        if order.price <= 0 or order.original_size <= 0:
            raise ReconciliationError("invalid recovered order price/size")
        if not (Decimal(0) <= order.remaining_size <= order.original_size):
            raise ReconciliationError("invalid remaining_size in recovered order")
        if not order.client_order_id.startswith("0x") or len(order.client_order_id) != 34:
            raise ReconciliationError("invalid recovered CLOID")

    @staticmethod
    def _slot(order: LiveOrder) -> tuple[str, Side]:
        return (order.instrument, order.side)

    @staticmethod
    def _matching(order: LiveOrder, intent: OrderIntent) -> bool:
        return (
            order.price == intent.price
            and order.original_size == intent.size
            and order.post_only == intent.post_only
            and order.reduce_only == intent.reduce_only
        )

    def _next_cloid(self, intent: OrderIntent) -> str:
        self._sequence += 1
        name = f"{self._session_id}:{self._sequence}:{intent.instrument}:{intent.side.value}"
        return "0x" + uuid5(NAMESPACE_URL, name).hex

    def resynchronize(
        self,
        open_orders: list[LiveOrder],
        *,
        positions_reconciled: bool,
        fills_reconciled: bool,
        confirmed_closed_cloids: frozenset[str] = frozenset(),
    ) -> bool:
        """Quarantine mismatches; never infer a confirmed close from absence.

        The caller verifies venue/account/DEX, obtains an authoritative snapshot,
        and independently reconciles fills and positions. A status-confirmed
        closed CLOID may be supplied in confirmed_closed_cloids. Foreign orders
        or a missing unresolved order prevent enabling new submissions.
        """
        self.start_recovery("resynchronization_in_progress")
        if not positions_reconciled or not fills_reconciled:
            self.blocked_reason = "position_or_fills_unverified"
            return False

        proposed = {cloid: replace(order) for cloid, order in self.live_orders.items()}
        observed: set[str] = set()
        slots: set[tuple[str, Side]] = set()
        try:
            for snapshot in open_orders:
                self._validate_order(snapshot)
                cloid = snapshot.client_order_id
                if cloid in observed or self._slot(snapshot) in slots:
                    raise ReconciliationError("duplicate venue order/slot")
                observed.add(cloid)
                slots.add(self._slot(snapshot))
                local = proposed.get(cloid)
                if local is None:
                    raise ReconciliationError("foreign open order: manual review required")
                if local.state in TERMINAL_STATES:
                    raise ReconciliationError("terminal local order is open on venue")
                if (local.instrument, local.side, local.price, local.original_size,
                    local.post_only, local.reduce_only) != (
                    snapshot.instrument, snapshot.side, snapshot.price,
                    snapshot.original_size, snapshot.post_only, snapshot.reduce_only,
                ):
                    raise ReconciliationError("venue/local identity or terms mismatch")
                if (local.exchange_order_id is not None and
                    snapshot.exchange_order_id != local.exchange_order_id):
                    raise ReconciliationError("CLOID/OID mismatch")
                if snapshot.remaining_size > local.remaining_size:
                    raise ReconciliationError("venue cumulative fill went backwards")
                # Pending cancels are not confirmed until the venue says closed.
                proposed[cloid] = replace(
                    local,
                    exchange_order_id=snapshot.exchange_order_id,
                    remaining_size=snapshot.remaining_size,
                    state=(OrderState.PENDING_CANCEL if local.state is OrderState.PENDING_CANCEL
                           else OrderState.PARTIALLY_FILLED if snapshot.filled_size > 0
                           else OrderState.RESTING),
                )

            for cloid, local in proposed.items():
                if local.active and cloid not in observed:
                    if cloid not in confirmed_closed_cloids:
                        raise ReconciliationError("local live/unknown order absent without confirmed status")
                    if local.state is OrderState.UNKNOWN and local.remaining_size < local.original_size:
                        # Recovering a partially filled unknown needs the fills ledger first.
                        pass
                    local.state = OrderState.CANCELLED
            # A confirmed close must not contradict an open snapshot.
            if confirmed_closed_cloids & observed:
                raise ReconciliationError("contradictory open and closed order evidence")
        except ReconciliationError as exc:
            self.blocked_reason = str(exc)
            return False

        self.live_orders = proposed
        self.ready = True
        self.blocked_reason = None
        return True

    def on_order_update(self, event: OrderEvent) -> Decimal:
        """Apply a validated normalized cumulative update. Return new fill delta.

        The delta is diagnostic only: portfolio accounting must ingest uniquely
        identified venue trade fills, not status update deltas.
        """
        if self.dex is not None and event.dex != self.dex:
            raise ValueError("cross-dex order update")
        if self.account_id is not None and (
            event.account_id is None or event.account_id.lower() != self.account_id.lower()
        ):
            raise ValueError("wrong-account order update")
        order = self.live_orders.get(event.client_order_id)
        if order is None:
            self.start_recovery("unknown_order_update")
            raise ReconciliationError("order update for unknown CLOID")
        if event.instrument is not None and event.instrument != order.instrument:
            self.start_recovery("mismatched_order_instrument")
            raise ReconciliationError("order update instrument mismatch")
        if (order.exchange_order_id is not None and event.exchange_order_id is not None
            and order.exchange_order_id != event.exchange_order_id):
            self.start_recovery("cloid_oid_mismatch")
            raise ReconciliationError("order update OID mismatch")

        previous_filled = order.filled_size
        cumulative = event.cumulative_filled_size
        if cumulative is not None:
            if not isinstance(cumulative, Decimal) or not cumulative.is_finite():
                self.start_recovery("invalid_cumulative_fill")
                raise ReconciliationError("cumulative fill must be finite Decimal")
            if not (Decimal(0) <= cumulative <= order.original_size):
                self.start_recovery("out_of_bounds_fill")
                raise ReconciliationError("cumulative fill exceeds original size")
            if cumulative > previous_filled:
                order.remaining_size = order.original_size - cumulative

        if order.exchange_order_id is None and event.exchange_order_id is not None:
            order.exchange_order_id = event.exchange_order_id

        kind = event.kind
        if kind is OrderEventType.UNKNOWN:
            order.state = OrderState.UNKNOWN
            self.start_recovery("ambiguous_order_action")
        elif order.state is OrderState.FILLED:
            pass  # Late/replayed status cannot reopen an already filled order.
        elif kind is OrderEventType.FILLED or order.remaining_size == 0:
            order.remaining_size = Decimal(0)
            order.state = OrderState.FILLED
        elif order.state is OrderState.CANCELLED:
            pass  # Late fill can update cumulative quantity but never reopen.
        elif kind is OrderEventType.CANCELLED:
            order.state = OrderState.CANCELLED
        elif kind is OrderEventType.REJECTED:
            if order.state is OrderState.PENDING_NEW and order.filled_size == 0:
                order.state = OrderState.REJECTED
            else:
                order.state = OrderState.UNKNOWN
                self.start_recovery("ambiguous_rejection")
        elif kind is OrderEventType.CANCEL_ACCEPTED:
            if order.active:
                order.state = OrderState.PENDING_CANCEL
        elif kind in (OrderEventType.RESTING, OrderEventType.PARTIAL_FILL):
            if order.state not in (OrderState.PENDING_CANCEL, OrderState.UNKNOWN):
                order.state = (OrderState.PARTIALLY_FILLED if order.filled_size > 0
                               else OrderState.RESTING)
        else:
            raise ValueError(f"unexpected order event type: {kind!r}")
        return order.filled_size - previous_filled

    def reconcile(self, desired: list[OrderIntent]) -> list[OrderAction]:
        """Reserve cancel-before-place actions. No SDK calls or implicit retries.

        New placements are disabled until explicit resynchronize() succeeds.
        A pending new/cancel or ambiguous order occupies its symbol+side slot.
        """
        targets: dict[tuple[str, Side], OrderIntent] = {}
        for intent in desired:
            self._validate_intent(intent)
            slot = (intent.instrument, intent.side)
            if slot in targets:
                raise ValueError("duplicate desired order for symbol+side")
            targets[slot] = intent

        if not self.ready:
            return []

        active: dict[tuple[str, Side], LiveOrder] = {}
        for order in self.live_orders.values():
            self._validate_order(order)
            if not order.active:
                continue
            slot = self._slot(order)
            if slot in active:
                self.start_recovery("multiple_live_orders_same_side")
                raise ReconciliationError("two live orders occupy one quote slot")
            active[slot] = order

        cancels: list[OrderAction] = []
        places: list[OrderAction] = []
        for slot in sorted(active.keys() | targets.keys(), key=lambda s: (s[0], s[1].value)):
            live = active.get(slot)
            target = targets.get(slot)
            if live is not None:
                if live.state in (OrderState.PENDING_NEW, OrderState.PENDING_CANCEL):
                    continue
                if live.state is OrderState.UNKNOWN:
                    self.start_recovery("unknown_live_order")
                    return []
                if target is None or not self._matching(live, target):
                    live.state = OrderState.PENDING_CANCEL
                    cancels.append(CancelAction(
                        instrument=live.instrument, client_order_id=live.client_order_id,
                        exchange_order_id=live.exchange_order_id,
                    ))
                continue
            if target is not None:
                allocated = replace(target, client_order_id=self._next_cloid(target))
                self.live_orders[allocated.client_order_id] = LiveOrder(
                    client_order_id=allocated.client_order_id,
                    exchange_order_id=None,
                    instrument=allocated.instrument,
                    side=allocated.side,
                    price=allocated.price,
                    original_size=allocated.size,
                    remaining_size=allocated.size,
                    state=OrderState.PENDING_NEW,
                    post_only=allocated.post_only,
                    reduce_only=allocated.reduce_only,
                )
                places.append(PlaceAction(allocated))
        return cancels + places

    def request_cancel_all(self) -> list[CancelAction]:
        """Emergency cancel of known resting orders even if quoting is disabled.

        Unknown/pending-new orders require status lookup first; they are never
        represented as safely cancelled. Calling twice never doubles requests.
        """
        actions: list[CancelAction] = []
        for order in self.live_orders.values():
            if order.state in (OrderState.RESTING, OrderState.PARTIALLY_FILLED):
                order.state = OrderState.PENDING_CANCEL
                actions.append(CancelAction(
                    order.instrument, order.client_order_id, order.exchange_order_id,
                ))
        return actions
