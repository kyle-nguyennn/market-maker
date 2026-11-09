"""Bridge existing market-maker quoters to the shared Strategy interface.

This file intentionally lives OUTSIDE alpha/ so alpha/ can be published as an
independent library without depending on market-maker's order and quoter types.
"""
from __future__ import annotations

from typing import Protocol

from alpha.strategy import QuoteProposal, StrategyContext, StrategyDecision
from model.market import MarketState
from model.order import Quote
from .base_quoter import BaseMMQuoter


class ContextualMMQuoter(Protocol):
    """Future quoter interface with access to features, signals and portfolio."""

    def quote_context(self, context: StrategyContext) -> list[Quote]:
        ...


class MarketMakingStrategy:
    """Market-making is one Strategy family, alongside directional strategies.

    A legacy BaseMMQuoter receives only MarketState. A contextual quoter can
    instead consume the entire StrategyContext. Both produce the same
    venue-independent QuoteProposal decision for downstream risk/execution.
    """

    def __init__(
        self, quoter: BaseMMQuoter | ContextualMMQuoter,
        *, strategy_id: str = "market_making",
    ) -> None:
        if not strategy_id or not strategy_id.strip():
            raise ValueError("strategy_id required")
        self.strategy_id = strategy_id
        self._quoter = quoter

    def decide(self, context: StrategyContext) -> StrategyDecision:
        if context.market is None or not context.market.is_valid:
            return StrategyDecision(self.strategy_id, context.decision_at_ms)
        if not isinstance(context.market, MarketState):
            raise TypeError("market-making adapter requires model.MarketState")
        if hasattr(self._quoter, "quote_context"):
            quotes = self._quoter.quote_context(context)
        else:
            quotes = self._quoter.quote(context.market)
        if not isinstance(quotes, list):
            raise TypeError("quoter must return list[Quote]")
        proposals = []
        for quote in quotes:
            if not isinstance(quote, Quote):
                raise TypeError("quoter must return Quote values")
            # Zero size is a disabled side; it cannot be a valid order.
            if quote.qty == 0:
                continue
            proposals.append(QuoteProposal(
                symbol=context.symbol, side=quote.side.value,
                price=quote.price, size=quote.qty,
            ))
        return StrategyDecision(
            strategy_id=self.strategy_id,
            decision_at_ms=context.decision_at_ms,
            quotes=tuple(proposals),
        )
