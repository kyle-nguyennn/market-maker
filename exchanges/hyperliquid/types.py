from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, NotRequired, TypedDict


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TimeInForce(str, Enum):
    GTC = "Gtc"
    IOC = "Ioc"
    ALO = "Alo"


@dataclass(frozen=True)
class HyperliquidMarketSpec:
    """Exchange precision metadata read from Hyperliquid meta."""

    sz_decimals: int


@dataclass(frozen=True)
class VenueOrderRequest:
    """Hyperliquid-facing order representation."""

    instrument: str
    side: OrderSide
    size: str
    price: str
    tif: TimeInForce = TimeInForce.ALO
    reduce_only: bool = False
    client_order_id: str | None = None


@dataclass(frozen=True)
class VenueOrderResult:
    """Hyperliquid-facing result before adapter normalization."""

    status: str
    order_id: int | None = None
    filled_size: str | None = None
    average_price: str | None = None
    error: str | None = None


class HyperliquidL2Level(TypedDict):
    """A single raw Hyperliquid L2 level; prices and sizes stay strings."""

    px: str
    sz: str
    n: NotRequired[int]


class HyperliquidL2Snapshot(TypedDict):
    """REST l2Book payload / data field of an l2Book WebSocket event."""

    coin: str
    time: int  # exchange Unix epoch milliseconds
    levels: list[list[HyperliquidL2Level]]  # [bids, asks], full snapshots


class HyperliquidL2BookMessage(TypedDict):
    """SDK WebSocket callback envelope for an l2Book subscription."""

    channel: Literal["l2Book"]
    data: HyperliquidL2Snapshot


def parse_l2_snapshot(raw: object) -> HyperliquidL2Snapshot:
    """Validate untrusted SDK JSON before exposing a typed wire response.

    This validates structure and wire scalar types. The market adapter owns
    Decimal conversion and order-book price/quantity validity checks.
    """
    if not isinstance(raw, dict):
        raise ValueError("Hyperliquid L2 snapshot must be an object")
    coin, timestamp, levels = raw.get("coin"), raw.get("time"), raw.get("levels")
    if not isinstance(coin, str) or not coin.strip():
        raise ValueError("Hyperliquid L2 snapshot missing coin")
    if type(timestamp) is not int or timestamp < 0:
        raise ValueError("Hyperliquid L2 snapshot time must be a nonnegative integer")
    if not isinstance(levels, list) or len(levels) != 2:
        raise ValueError("Hyperliquid L2 snapshot must contain bid and ask arrays")

    normalized: list[list[HyperliquidL2Level]] = []
    for side in levels:
        if not isinstance(side, list):
            raise ValueError("Hyperliquid L2 side must be an array")
        parsed_side: list[HyperliquidL2Level] = []
        for level in side:
            if not isinstance(level, dict):
                raise ValueError("Hyperliquid L2 level must be an object")
            px, sz = level.get("px"), level.get("sz")
            if not isinstance(px, str) or not isinstance(sz, str):
                raise ValueError("Hyperliquid L2 px and sz must be strings")
            parsed_level: HyperliquidL2Level = {"px": px, "sz": sz}
            if "n" in level:
                count = level["n"]
                if type(count) is not int or count < 0:
                    raise ValueError("Hyperliquid L2 level n must be a nonnegative integer")
                parsed_level["n"] = count
            parsed_side.append(parsed_level)
        normalized.append(parsed_side)
    return {"coin": coin, "time": timestamp, "levels": normalized}


def parse_l2_book_message(raw: object) -> HyperliquidL2BookMessage:
    """Validate the SDK callback envelope; ignore no unexpected channels."""
    if not isinstance(raw, dict) or raw.get("channel") != "l2Book":
        raise ValueError("expected Hyperliquid l2Book WebSocket message")
    return {"channel": "l2Book", "data": parse_l2_snapshot(raw.get("data"))}
