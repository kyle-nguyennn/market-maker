"""Pure, independently publishable numeric feature calculators.

No source, history store, ingestion clock, strategy, execution, or venue imports.
"""
from __future__ import annotations

from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Mapping, Protocol, Sequence

from .observations import PriceBar


class FeatureCalculator(Protocol):
    """Compute deterministic feature values from a vetted chronological window."""

    def calculate(self, history: Sequence[PriceBar]) -> Mapping[str, Decimal]:
        ...


class PriceFeatureCalculator:
    """Reference OHLCV price statistics, computed with fixed Decimal context.

    Caller must validate source, freshness, availability, ordering and gaps.
    This class deliberately has no I/O and no mutable history.
    The input window must contain at least three bars.
    """

    def calculate(self, history: Sequence[PriceBar]) -> Mapping[str, Decimal]:
        if len(history) < 3:
            raise ValueError("feature calculation requires at least three price bars")
        if any(not isinstance(bar, PriceBar) for bar in history):
            raise TypeError("feature calculation requires normalized PriceBar inputs")
        with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
            prices = [bar.close for bar in history]
            unit = Decimal("1")
            returns = [
                prices[i] / prices[i - 1] - unit
                for i in range(1, len(prices))
            ]
            n = Decimal(len(returns))
            mean_return = sum(returns, Decimal("0")) / n
            return_var = sum(
                ((r - mean_return) ** 2 for r in returns), Decimal("0")
            ) / n

            recent = prices[1:]
            count = Decimal(len(recent))
            mean_price = sum(recent, Decimal("0")) / count
            price_var = sum(
                ((p - mean_price) ** 2 for p in recent), Decimal("0")
            ) / count

            return {
                "return_1": returns[-1],
                "momentum": prices[-1] / prices[0] - unit,
                "realized_vol": return_var.sqrt(),
                "price_zscore": (
                    (prices[-1] - mean_price) / price_var.sqrt()
                    if price_var != 0 else Decimal("0")
                ),
            }
