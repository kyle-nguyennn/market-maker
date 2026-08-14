from typing import List
from enum import Enum
from dataclasses import dataclass
from abc import ABC, abstractmethod

from market_handler.market_adapter import MarketState

class Side(Enum):
    BID = 'BID'
    ASK = 'ASK'

@dataclass
class Quote:
    price: int
    qty: int
    side: Side

class BaseQuoter(ABC):
    """
        Quoter should return `desired` quote.
        Decision to requote is handle in the execution/control layer
    """
    @abstractmethod
    def quote(self, state: MarketState) -> List[Quote]:
        pass

class BaseMMQuoter(BaseQuoter):
    @abstractmethod
    def fair_price(self, state: MarketState) -> int:
        pass

    @abstractmethod
    def reservation_price(self, state: MarketState, fair: int) -> int:
        pass

    @abstractmethod
    def calculate_spread(self, state: MarketState, fair: int, reservation: int) -> tuple[int, int]:
        pass

    @abstractmethod
    def calculate_size(self, state: MarketState, bid_price: int, ask_price: int) -> tuple[int, int]:
        pass

    def quote(self, state: MarketState) -> List[Quote]:
        fair = self.fair_price(state)
        reservation = self.reservation_price(state, fair)
        bid_offset, ask_offset = self.calculate_spread(state, fair, reservation)

        bid_price, ask_price = reservation - bid_offset, reservation + ask_offset
        bid_size, ask_size = self.calculate_size(state, bid_price, ask_price)
        return [
            Quote(bid_price, bid_size, Side.BID),
            Quote(ask_price, ask_size, Side.ASK),
        ]