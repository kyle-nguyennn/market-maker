from abc import ABC, abstractmethod
from decimal import Decimal

from model.market import MarketState
from model.order import Quote, Side


class BaseQuoter(ABC):
    """Pure strategy interface.

    Quoters operate only on venue-independent Decimal domain objects. They do
    not round to ticks, serialize prices, or know exchange-specific symbols.
    """

    @abstractmethod
    def quote(self, state: MarketState) -> list[Quote]:
        pass


class BaseMMQuoter(BaseQuoter):
    @abstractmethod
    def fair_price(self, state: MarketState) -> Decimal:
        pass

    @abstractmethod
    def reservation_price(self, state: MarketState, fair: Decimal) -> Decimal:
        pass

    @abstractmethod
    def calculate_spread(
        self,
        state: MarketState,
        fair: Decimal,
        reservation: Decimal,
    ) -> tuple[Decimal, Decimal]:
        pass

    @abstractmethod
    def calculate_size(
        self,
        state: MarketState,
        bid_price: Decimal,
        ask_price: Decimal,
    ) -> tuple[Decimal, Decimal]:
        pass

    def quote(self, state: MarketState) -> list[Quote]:
        fair = self.fair_price(state)
        reservation = self.reservation_price(state, fair)
        bid_offset, ask_offset = self.calculate_spread(state, fair, reservation)

        bid_price = reservation - bid_offset
        ask_price = reservation + ask_offset
        bid_size, ask_size = self.calculate_size(state, bid_price, ask_price)

        return [
            Quote(bid_price, bid_size, Side.BID),
            Quote(ask_price, ask_size, Side.ASK),
        ]
