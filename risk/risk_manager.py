from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RiskLimits:
    max_abs_position: Decimal
    max_order_notional: Decimal
    max_abs_notional: Decimal
    max_drawdown: Decimal
    max_market_data_age_ms: int


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = "ok"


class RiskManager:
    """Hard limits must remain independent of the quoting model."""

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self.kill_switch = False

    def evaluate_market_age(self, age_ms: int) -> RiskDecision:
        if self.kill_switch:
            return RiskDecision(False, "kill_switch")
        if age_ms > self.limits.max_market_data_age_ms:
            return RiskDecision(False, "stale_market_data")
        return RiskDecision(True)

    def evaluate_order_notional(self, price: Decimal, size: Decimal) -> RiskDecision:
        if self.kill_switch:
            return RiskDecision(False, "kill_switch")
        if abs(price * size) > self.limits.max_order_notional:
            return RiskDecision(False, "max_order_notional")
        return RiskDecision(True)

    def trigger_kill_switch(self) -> None:
        self.kill_switch = True
