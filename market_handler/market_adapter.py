"""Venue adapter contract.

The internal model uses Decimal. Each venue adapter owns conversion between
wire/SDK types and the venue-independent model in both directions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from model.order import ExecutionReport, OrderIntent


class BaseMarketAdapter(ABC):
    @abstractmethod
    def to_internal_instrument(self, venue_instrument: str) -> str:
        """Map a venue symbol into the logical internal instrument name."""

    @abstractmethod
    def to_venue_instrument(self, instrument: str) -> str:
        """Map a logical internal instrument name into the venue symbol."""

    @abstractmethod
    def to_venue_order(self, intent: OrderIntent, market_spec: Any) -> Any:
        """Convert a Decimal internal intent into the exact venue request."""

    @abstractmethod
    def to_internal_execution_report(self, venue_result: Any) -> ExecutionReport:
        """Convert an exchange result into the internal Decimal representation."""
