from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class PortfolioState:
    cash: Decimal = Decimal("0")
    position: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")

    def apply_fill(
        self,
        *,
        side: str,
        price: Decimal,
        size: Decimal,
        fee: Decimal = Decimal("0"),
    ) -> None:
        """Apply one fill using BUY-positive / SELL-negative inventory."""
        signed_size = size if side.upper() == "BUY" else -size
        self.position += signed_size
        self.cash -= signed_size * price
        self.cash -= fee
        self.fees_paid += fee

    def marked_equity(self, mark_price: Decimal) -> Decimal:
        return self.cash + self.position * mark_price
