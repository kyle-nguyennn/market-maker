"""Venue-independent internal trading model.

All prices, sizes, notionals, rates, and monetary values use Decimal internally.
Venue adapters own conversion to and from exchange-specific representations.
"""

from .market import BookLevel, MarketState
from .order import (
    ExecutionReport,
    ExecutionStatus,
    OrderIntent,
    Quote,
    Side,
)

__all__ = [
    "BookLevel",
    "ExecutionReport",
    "ExecutionStatus",
    "MarketState",
    "OrderIntent",
    "Quote",
    "Side",
]
