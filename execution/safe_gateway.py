"""Order actions may enter the SDK only through this fail-closed boundary.

Importantly, this module NEVER enables live trading on its own. Its default is
paper/dry-run. The Hyperliquid client retains the separate scored-pas signing
guard. The caller owns fresh account snapshots, journaling and request budgets.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable

from execution.order_manager import OrderManager, OrderState
from model.order import (
    CancelAction, ExecutionStatus, OrderAction, OrderEvent,
    OrderEventType, PlaceAction,
)
from risk.risk_manager import OpenExposure, RiskDecision, RiskManager, RiskSnapshot


@dataclass(frozen=True)
class GatewayOutcome:
    action: OrderAction
    sent: bool
    allowed: bool
    reason: str


class SafeExecutionGateway:
    """Final adapter-quantized, account-aware pre-send validation.

    Live execution requires two separate opt-ins: live_enabled and
    operator_authorized. This is NOT a complete continuous execution runner:
    persistence, API budgets, capacity normalization and startup orchestration
    remain mandatory before enabling live mode.
    """

    def __init__(
        self, *, manager: OrderManager, risk: RiskManager,
        adapter: Any, client: Any, market_spec: Any,
        live_enabled: bool = False, operator_authorized: bool = False,
    ) -> None:
        if live_enabled and not operator_authorized:
            raise PermissionError("live execution requires explicit operator authorization")
        if live_enabled and (not manager.dex or not manager.account_id):
            raise PermissionError("live execution requires pinned DEX and account identity")
        self.manager = manager
        self.risk = risk
        self.adapter = adapter
        self.client = client
        self.market_spec = market_spec
        self.live_enabled = live_enabled

    def _event(
        self, cloid: str, kind: OrderEventType,
        *, oid: int | None = None, cumulative: Decimal | None = None,
        instrument: str | None = None, reason: str | None = None,
    ) -> OrderEvent:
        return OrderEvent(
            cloid, kind, exchange_order_id=oid,
            cumulative_filled_size=cumulative, instrument=instrument,
            dex=self.manager.dex, account_id=self.manager.account_id, reason=reason,
        )

    def _block(self, action: PlaceAction, reason: str) -> GatewayOutcome:
        self.manager.on_order_update(self._event(
            action.intent.client_order_id, OrderEventType.REJECTED,
            instrument=action.intent.instrument, reason=reason,
        ))
        return GatewayOutcome(action, False, False, reason)

    def dispatch(
        self, action: OrderAction,
        snapshot_provider: Callable[[], RiskSnapshot],
    ) -> GatewayOutcome:
        """Dispatch one action; snapshot_provider is called just before a place.

        Caller MUST journal the CLOID/action before this call. Ambiguous order
        submission/cancel exceptions trigger UNKNOWN, never a blind retry.
        Cancels are allowed while kill-switched or quoting disabled.
        """
        if isinstance(action, CancelAction):
            order = self.manager.live_orders.get(action.client_order_id)
            if order is None or order.instrument != action.instrument or (
                order.state is not OrderState.PENDING_CANCEL
            ):
                return GatewayOutcome(action, False, False, "invalid_cancel_reservation")
            if not self.live_enabled:
                return GatewayOutcome(action, False, True, "dry_run_cancel_no_sdk")
            try:
                success = self.client.cancel_order(
                    action.instrument, client_order_id=action.client_order_id
                )
            except Exception:
                self.manager.on_order_update(self._event(
                    action.client_order_id, OrderEventType.UNKNOWN,
                    instrument=action.instrument, reason="cancel_send_uncertain",
                ))
                return GatewayOutcome(action, True, False, "cancel_status_unknown")
            if success is not True:
                self.manager.on_order_update(self._event(
                    action.client_order_id, OrderEventType.UNKNOWN,
                    instrument=action.instrument, reason="cancel_response_uncertain",
                ))
                return GatewayOutcome(action, True, False, "cancel_status_unknown")
            self.manager.on_order_update(self._event(
                action.client_order_id, OrderEventType.CANCEL_ACCEPTED,
                instrument=action.instrument,
            ))
            return GatewayOutcome(action, True, True, "cancel_accepted_not_terminal")

        if not isinstance(action, PlaceAction):
            raise TypeError("unsupported order action")
        intent = action.intent
        if not intent.client_order_id:
            return GatewayOutcome(action, False, False, "missing_cloid")
        reserved = self.manager.live_orders.get(intent.client_order_id)
        if not self.manager.ready or reserved is None or reserved.state is not OrderState.PENDING_NEW:
            return GatewayOutcome(action, False, False, "unreconciled_or_unreserved_order")
        if (
            reserved.instrument != intent.instrument or reserved.side != intent.side
            or reserved.price != intent.price or reserved.original_size != intent.size
            or reserved.post_only != intent.post_only or reserved.reduce_only != intent.reduce_only
        ):
            return GatewayOutcome(action, False, False, "order_reservation_mismatch")
        if self.risk.kill_switch:
            return self._block(action, "kill_switch")
        try:
            request = self.adapter.to_venue_order(intent, self.market_spec)
            price = Decimal(request.price)
            size = Decimal(request.size)
        except Exception:
            return self._block(action, "venue_quantization_failed")
        # Prevent live/local-state divergence: the manager must reconcile the
        # already-quantized target, NOT an unrounded strategy price/size.
        if price != intent.price or size != intent.size:
            return self._block(action, "intent_must_be_venue_quantized")
        try:
            snap = snapshot_provider()
        except Exception:
            return self._block(action, "fresh_snapshot_unavailable")
        if (
            snap.dex != self.manager.dex
            or not self.manager.account_id
            or not snap.account_id
            or snap.account_id.lower() != self.manager.account_id.lower()
            or request.instrument != f"{snap.dex}:{intent.instrument}"
            or snap.expected_account_id is None
            or snap.expected_account_id.lower() != self.manager.account_id.lower()
        ):
            return self._block(action, "gateway_account_or_dex_mismatch")
        # Never trust a caller-supplied exposure list: source fillable orders
        # directly from the manager, including pending new AND pending cancel.
        exposures = tuple(
            OpenExposure(o.instrument, o.side, o.remaining_size, o.client_order_id)
            for o in self.manager.live_orders.values() if o.active
        )
        snap = replace(snap, fillable_orders=exposures)
        decision: RiskDecision = self.risk.evaluate_placement(
            replace(intent, price=price, size=size), snap
        )
        if not decision.allowed:
            return self._block(action, decision.reason)
        if not self.live_enabled:
            return GatewayOutcome(action, False, True, "dry_run_validated_no_sdk")
        try:
            result = self.client.place_order(request)
            report = self.adapter.to_internal_execution_report(result)
        except Exception:
            self.manager.on_order_update(self._event(
                intent.client_order_id, OrderEventType.UNKNOWN,
                instrument=intent.instrument, reason="submit_response_uncertain",
            ))
            return GatewayOutcome(action, True, False, "submit_status_unknown")
        kind = {
            ExecutionStatus.RESTING: OrderEventType.RESTING,
            ExecutionStatus.FILLED: OrderEventType.FILLED,
            ExecutionStatus.REJECTED: OrderEventType.REJECTED,
        }[report.status]
        self.manager.on_order_update(self._event(
            intent.client_order_id, kind, oid=report.exchange_order_id,
            cumulative=report.filled_size, instrument=intent.instrument,
            reason=report.error,
        ))
        return GatewayOutcome(action, True, report.status is not ExecutionStatus.REJECTED,
                              report.status.value.lower())

    def dispatch_many(
        self, actions: list[OrderAction],
        snapshot_provider: Callable[[], RiskSnapshot],
    ) -> list[GatewayOutcome]:
        """Order cancels before new requests irrespective of caller list order."""
        cancels = [x for x in actions if isinstance(x, CancelAction)]
        places = [x for x in actions if isinstance(x, PlaceAction)]
        if len(cancels) + len(places) != len(actions):
            raise TypeError("unsupported order action in batch")
        return [self.dispatch(x, snapshot_provider) for x in cancels + places]
