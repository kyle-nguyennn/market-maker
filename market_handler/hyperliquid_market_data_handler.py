"""Read-only, stateful L2 market-data cache for a single Hyperliquid instrument.

Hyperliquid l2Book WS events are full snapshots, not incremental deltas.
The venue adapter remains a pure converter; this handler owns current state,
message ordering, freshness, and subscription lifecycle. No signed actions.
"""
from __future__ import annotations

import time
from dataclasses import replace
from threading import RLock
from typing import Callable, Literal

from exchanges.hyperliquid.client import HyperliquidVenueClient
from exchanges.hyperliquid.types import (
    HyperliquidL2Snapshot,
    parse_l2_book_message,
)
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter
from model.market import BookLevel, MarketState


class HyperliquidMarketDataHandler:
    """Cache one logical market. Create one instance per instrument.

    `current()` returns an immutable snapshot, flagged invalid for a stale,
    future-dated, malformed, or disconnected feed. Never use cached bids/asks
    alone as proof that a quote is safe to place.

    There is no wire sequence number in l2Book. We reject strictly older
    exchange timestamps and retain SDK arrival order for equal timestamps.
    This is not a guarantee of gap-free delivery or automatic reconnection.
    """

    def __init__(
        self,
        client: HyperliquidVenueClient,
        instrument: str,
        *,
        adapter: HyperliquidMarketAdapter | None = None,
        max_age_ms: int = 2_000,
        max_future_skew_ms: int = 1_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if max_age_ms <= 0 or max_future_skew_ms < 0:
            raise ValueError("invalid market data freshness configuration")
        self.client = client
        self.adapter = adapter or HyperliquidMarketAdapter(client.config)
        if self.adapter.config != client.config:
            raise ValueError("adapter and client must share the same DEX profile")
        self.instrument = self.adapter.to_internal_instrument(instrument)
        self.venue_instrument = self.adapter.to_venue_instrument(self.instrument)
        self.max_age_ms = max_age_ms
        self.max_future_skew_ms = max_future_skew_ms
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._lock = RLock()
        self._state: MarketState | None = None
        self._source: Literal["rest", "ws"] | None = None
        self._blocked_reason: str | None = "uninitialized"
        self._running = False
        self._subscription_id: int | None = None
        self._generation = 0  # reject delayed callbacks from prior subscriptions

    def current(self) -> MarketState | None:
        """Return a safe read-only view; stale cached levels remain visible but invalid."""
        with self._lock:
            state = self._state
            reason = self._blocked_reason
        if state is None:
            return None
        now = self._clock_ms()
        if state.timestamp_ms > now + self.max_future_skew_ms:
            reason = "future_book"
        elif now - state.timestamp_ms > self.max_age_ms:
            reason = "stale_book"
        if reason is not None:
            return replace(state, is_valid=False, invalid_reason=reason)
        return state

    @property
    def bids(self) -> tuple[BookLevel, ...]:
        """Latest cached bids, even if stale. Check current().is_valid before use."""
        state = self.current()
        return state.bids if state is not None else ()

    @property
    def asks(self) -> tuple[BookLevel, ...]:
        state = self.current()
        return state.asks if state is not None else ()

    @property
    def ready(self) -> bool:
        state = self.current()
        return state is not None and state.is_valid

    def invalidate(self, reason: str = "resync_required") -> None:
        """Fail closed until a fresh, valid snapshot is accepted."""
        if not reason:
            raise ValueError("invalid data reason must be nonempty")
        with self._lock:
            self._blocked_reason = reason

    def apply_snapshot(
        self,
        book: HyperliquidL2Snapshot,
        *,
        source: Literal["rest", "ws"] = "ws",
    ) -> MarketState:
        """Validate and atomically replace the latest full-depth book.

        Never mutate the existing MarketState; readers holding older snapshots
        see a stable view. Older timestamps must not roll the book backwards.
        """
        if source not in ("rest", "ws"):
            raise ValueError("unknown L2 source")
        try:
            state = self.adapter.from_sdk_l2_snapshot(book)
            if state.instrument != self.instrument:
                raise ValueError("L2 book belongs to a different instrument/DEX")
        except Exception:
            self.invalidate("invalid_book")
            raise
        if state.timestamp_ms > self._clock_ms() + self.max_future_skew_ms:
            self.invalidate("future_book")
            raise ValueError("L2 book timestamp is in the future")
        with self._lock:
            if self._state is not None:
                if state.timestamp_ms < self._state.timestamp_ms:
                    return self.current()  # no freshness extension on old data
                if (state.timestamp_ms == self._state.timestamp_ms
                        and source == "rest" and self._source == "ws"):
                    return self.current()  # don't overwrite a WS frame with REST
            self._state = state
            self._source = source
            self._blocked_reason = None
        return self.current()

    def handle_ws_message(self, message: object) -> MarketState:
        """Accept exactly the SDK callback envelope {channel:l2Book,data:...}."""
        envelope = parse_l2_book_message(message)
        return self.apply_snapshot(envelope["data"], source="ws")

    def _on_ws_message(self, message: object, generation: int) -> None:
        # The SDK invokes callbacks on its WS thread; never crash that thread
        # or silently keep quoting using an older apparently-valid book.
        # Keep the lifecycle check and state publication atomic with stop().
        with self._lock:
            if not self._running or generation != self._generation:
                return
            try:
                self.handle_ws_message(message)
            except Exception:
                self._blocked_reason = "invalid_ws_book"

    def refresh(self, *, _generation: int | None = None) -> MarketState:
        """Fetch a read-only REST snapshot, with WS precedence for newer data."""
        try:
            book = self.client.query_l2_book(self.instrument)
            with self._lock:
                if _generation is not None and (
                    not self._running or self._generation != _generation
                ):
                    raise RuntimeError("REST bootstrap superseded by stop/restart")
                return self.apply_snapshot(book, source="rest")
        except Exception:
            # A failed concurrent bootstrap must not override a restarted
            # generation's data-quality status.
            with self._lock:
                if _generation is None or self._generation == _generation:
                    self._blocked_reason = "rest_refresh_failed"
            raise

    def start(self) -> MarketState:
        """Subscribe first, then REST bootstrap to avoid the subscribe/fetch gap.

        If a newer WS frame arrives while REST is in flight, it wins.
        No private-key, account query, or authenticated trading action is used.
        """
        with self._lock:
            if self._running or self._subscription_id is not None:
                raise RuntimeError("market data handler is already subscribed")
            self._running = True
            self._generation += 1
            generation = self._generation
            self._blocked_reason = "awaiting_bootstrap"
        try:
            sub_id = self.client.subscribe_public(
                "l2Book", self.instrument,
                lambda message: self._on_ws_message(message, generation),
            )
            with self._lock:
                self._subscription_id = sub_id
            return self.refresh(_generation=generation)
        except Exception:
            with self._lock:
                still_current = generation == self._generation
                if still_current:
                    self._blocked_reason = "market_data_start_failed"
            if still_current:
                try:
                    self.stop()
                except Exception:
                    pass  # leave disabled, expose unsubscribe failure on next start
            raise

    def stop(self) -> None:
        """Mark offline and unsubscribe only our L2 feed, not the shared SDK WS."""
        with self._lock:
            self._running = False
            self._blocked_reason = "stream_stopped"
            sub_id = self._subscription_id
        if sub_id is not None:
            self.client.unsubscribe_public("l2Book", self.instrument, sub_id)
            with self._lock:
                self._subscription_id = None
