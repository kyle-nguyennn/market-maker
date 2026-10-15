from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class PortfolioState:
    """Single-instrument cash/inventory proxy; NOT full perpetual-margin equity.

    Account equity, funding, cross-market PnL and margin-mode capacity MUST be
    read from a separately reconciled account snapshot before live risk checks.
    The venue fill ID should be scoped to DEX/account/instrument by the caller.
    """
    cash: Decimal = Decimal("0")
    position: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    seen_fill_ids: set[str] = field(default_factory=set, repr=False)

    def apply_fill(
        self, *, side: str, price: Decimal, size: Decimal,
        fee: Decimal = Decimal("0"), fill_id: str | None = None,
    ) -> bool:
        """Apply a unique trade fill once; return False for replay/duplicate.

        'size' is per-trade delta (not order cumulative fill). A distinct
        non-empty fill_id is required to make REST+WebSocket replay idempotent.
        Legacy callers may omit fill_id, but then deduplication is impossible.
        A negative finite fee is permitted for maker rebates.
        """
        direction = side.upper()
        if direction not in ("BUY", "SELL"):
            raise ValueError("fill side must be BUY or SELL")
        if (not isinstance(price, Decimal) or not price.is_finite() or price <= 0
            or not isinstance(size, Decimal) or not size.is_finite() or size <= 0
            or not isinstance(fee, Decimal) or not fee.is_finite()):
            raise ValueError("fill price, size and fee must be finite Decimal; price/size positive")
        if fill_id is not None and not fill_id:
            raise ValueError("fill ID cannot be empty")
        if fill_id is not None and fill_id in self.seen_fill_ids:
            return False

        signed_size = size if direction == "BUY" else -size
        self.position += signed_size
        self.cash -= signed_size * price
        self.cash -= fee
        self.fees_paid += fee
        if fill_id is not None:
            self.seen_fill_ids.add(fill_id)
        return True

    def marked_equity(self, mark_price: Decimal) -> Decimal:
        if not isinstance(mark_price, Decimal) or not mark_price.is_finite() or mark_price <= 0:
            raise ValueError("mark price must be a positive finite Decimal")
        return self.cash + self.position * mark_price
