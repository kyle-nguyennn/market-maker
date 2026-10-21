"""Conservative, venue-neutral safety gate and state supervisor.

The supervisor never sends orders. The actual dispatcher MUST call
authorize_placement immediately before every network placement, and must
persist planned CLOIDs independently of this best-effort telemetry.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from time import monotonic
from typing import Any, Callable

from execution.order_manager import OrderManager
from model.order import CancelAction, OrderIntent
from observability.events import EventScope
from observability.reconciliation import ReconcileResult
from risk.risk_manager import RiskDecision


class TradingState(str, Enum):
    PAUSED = "PAUSED"
    ACTIVE = "ACTIVE"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class SafetyPolicy:
    max_feed_age_s: float
    max_heartbeat_age_s: float
    max_reconciliation_age_s: float

    def __post_init__(self) -> None:
        from math import isfinite
        for name in (
            "max_feed_age_s", "max_heartbeat_age_s", "max_reconciliation_age_s",
        ):
            value = getattr(self, name)
            if not isinstance(value, (float, int)) or not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")


class SafetySupervisor:
    """Recovery/arming requires fresh feed, heartbeat and confirmed sync.

    A pause irreversibly invalidates the previous reconciliation generation.
    New placements require explicit operator authorization to re-arm.
    """

    def __init__(
        self,
        manager: OrderManager,
        scope: EventScope,
        policy: SafetyPolicy,
        *,
        pretrade_check: Callable[[OrderIntent, Any], RiskDecision],
        risk_killed: Callable[[], bool],
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.manager = manager
        self.scope = scope
        self.policy = policy
        self.pretrade_check = pretrade_check
        self.risk_killed = risk_killed
        self.clock = clock
        self.state = TradingState.PAUSED
        self.reason = "startup_reconciliation_required"
        self._feed_at: float | None = None
        self._heartbeat_at: float | None = None
        self._reconciled_at: float | None = None
        self.manager.start_recovery(self.reason)

    def pause(self, reason: str) -> None:
        if self.state is TradingState.STOPPED:
            return
        self.state = TradingState.PAUSED
        self.reason = reason
        self._reconciled_at = None
        self.manager.start_recovery(reason)

    def stop(self, reason: str = "operator_stop") -> None:
        self.pause(reason)
        self.state = TradingState.STOPPED

    def _accept_signal(self, scope: EventScope) -> bool:
        if not self.scope.same_account(scope):
            self.pause("wrong_signal_scope")
            return False
        return True

    def on_bot_heartbeat(self, scope: EventScope) -> None:
        if self._accept_signal(scope) and self.state is not TradingState.STOPPED:
            self._heartbeat_at = self.clock()

    def on_market_update(self, scope: EventScope, *, valid: bool) -> None:
        if not self._accept_signal(scope) or self.state is TradingState.STOPPED:
            return
        if not valid:
            self.pause("invalid_market_data")
            return
        self._feed_at = self.clock()

    def on_reconciled(self, scope: EventScope, result: ReconcileResult) -> None:
        if not self._accept_signal(scope) or self.state is TradingState.STOPPED:
            return
        if not result.verified or not self.manager.ready:
            self.pause("reconciliation_failed:" + result.reason)
            return
        self._reconciled_at = self.clock()

    def _health(self) -> RiskDecision:
        now = self.clock()
        for label, instant, limit in (
            ("feed", self._feed_at, self.policy.max_feed_age_s),
            ("heartbeat", self._heartbeat_at, self.policy.max_heartbeat_age_s),
            ("reconciliation", self._reconciled_at, self.policy.max_reconciliation_age_s),
        ):
            if instant is None:
                return RiskDecision(False, f"missing_{label}")
            if now < instant or now - instant > limit:
                return RiskDecision(False, f"stale_{label}")
        if not self.manager.ready:
            return RiskDecision(False, "order_state_unreconciled")
        if self.risk_killed():
            return RiskDecision(False, "risk_kill_switch")
        return RiskDecision(True)

    def arm(self, *, operator_authorized: bool) -> RiskDecision:
        if self.state is TradingState.STOPPED:
            return RiskDecision(False, "stopped")
        if not operator_authorized:
            return RiskDecision(False, "operator_authorization_required")
        verdict = self._health()
        if not verdict.allowed:
            self.pause(verdict.reason)
            return verdict
        self.state = TradingState.ACTIVE
        self.reason = "ok"
        return RiskDecision(True)

    def check(self) -> RiskDecision:
        if self.state is not TradingState.ACTIVE:
            return RiskDecision(False, self.reason)
        verdict = self._health()
        if not verdict.allowed:
            self.pause(verdict.reason)
        return verdict

    def authorize_placement(self, intent: OrderIntent, risk_snapshot: Any) -> RiskDecision:
        """To be called by the real send gateway, not just quote planning.

        pretrade_check must perform the venue's synchronous, complete risk
        evaluation. For Hyperliquid use RiskManager.evaluate_placement and
        supply risk_killed=lambda: risk_manager.kill_switch.
        """
        safety = self.check()
        if not safety.allowed:
            return safety
        verdict = self.pretrade_check(intent, risk_snapshot)
        if not isinstance(verdict, RiskDecision):
            self.pause("invalid_risk_response")
            return RiskDecision(False, "invalid_risk_response")
        if self.risk_killed():
            self.pause("risk_kill_switch")
            return RiskDecision(False, "risk_kill_switch")
        return verdict

    def request_safe_cancels(self) -> list[CancelAction]:
        """Plan cancels of known orders only; NOT evidence they were cancelled."""
        return self.manager.request_cancel_all()
