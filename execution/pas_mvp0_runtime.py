"""MVP-0: one-process PAS trading coordinator.

One OMS owner thread performs ALL OrderManager mutations; SQLite has its own
writer thread and the quoter has its own worker. The SDK handles WebSocket
callbacks only in the thread-safe market data cache. Not a high-frequency bot.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable

from execution.order_manager import LiveOrder, OrderManager, OrderState, ReconciliationError
from execution.safe_gateway import SafeExecutionGateway
from execution.sqlite_journal import SQLiteWALJournal
from exchanges.hyperliquid.client import HyperliquidVenueClient
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter
from market_handler.hyperliquid_market_data_handler import HyperliquidMarketDataHandler
from model.order import CancelAction, OrderIntent, PlaceAction, Side
from risk.risk_manager import RiskManager, RiskSnapshot

LOG = logging.getLogger(__name__)
TERMINAL_VENUE_STATUSES = frozenset({"filled", "canceled", "rejected"})


def dec(value: Any) -> Decimal:
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise ReconciliationError("unrecognized numeric venue payload")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ReconciliationError("nonfinite venue numeric field")
    return result


def positions_from_perps(perps: dict) -> dict[str, Decimal]:
    if not isinstance(perps, dict) or not isinstance(perps.get("assetPositions"), list):
        raise ReconciliationError("missing authoritative assetPositions")
    positions: dict[str, Decimal] = {}
    for row in perps["assetPositions"]:
        pos = row["position"]
        coin = pos["coin"]
        if not isinstance(coin, str) or not coin:
            raise ReconciliationError("invalid position coin")
        if coin in positions:
            raise ReconciliationError("duplicate position coin")
        positions[coin] = dec(pos["szi"])
    return positions


def venue_status(raw: dict) -> tuple[str, int | None]:
    """Only accept documented orderStatus envelope, never guess on missing keys."""
    if not isinstance(raw, dict) or raw.get("status") != "order":
        raise ReconciliationError("unresolved order status response")
    record = raw.get("order")
    if not isinstance(record, dict):
        raise ReconciliationError("missing orderStatus record")
    status = record.get("status")
    body = record.get("order")
    if not isinstance(status, str) or not isinstance(body, dict):
        raise ReconciliationError("incomplete orderStatus record")
    oid = body.get("oid")
    if not isinstance(oid, int) or isinstance(oid, bool) or oid < 0:
        raise ReconciliationError("orderStatus missing OID")
    return status.lower(), oid


@dataclass(frozen=True)
class VerifiedAccount:
    positions: dict[str, Decimal]
    equity: Decimal
    open_orders: tuple[LiveOrder, ...]
    capacity_buy: Decimal
    capacity_sell: Decimal
    mark: Decimal


class PassiveQuoter:
    """Deliberately simple static edge; risk gate handles inventory limits."""
    def __init__(self, *, size: Decimal, edge_bps: Decimal) -> None:
        if size <= 0 or edge_bps <= 0:
            raise ValueError("quote size and edge must be positive")
        self.size, self.edge_bps = size, edge_bps

    def desired(self, book: Any) -> list[OrderIntent]:
        if book is None or not book.is_valid or book.mid is None:
            return []
        edge = book.mid * self.edge_bps / Decimal(10000)
        return [
            OrderIntent(book.instrument, Side.BID, book.best_bid - edge, self.size),
            OrderIntent(book.instrument, Side.ASK, book.best_ask + edge, self.size),
        ]


class MVP0PasBot:
    """Synchronous OMS ownership, background Quoter and SQLite writer threads.

    Every boot gates on actual exchange state AND the journal's signed fill
    projection from an explicitly flat activation point. Any ambiguity halts.
    """
    def __init__(
        self, *, client: HyperliquidVenueClient, adapter: HyperliquidMarketAdapter,
        market: HyperliquidMarketDataHandler, manager: OrderManager,
        gateway: SafeExecutionGateway, risk: RiskManager, journal: SQLiteWALJournal,
        team_wallet: str, instrument: str, quoter: PassiveQuoter,
        live: bool = False, verified_unified: bool = False,
        poll_s: float = 2.0, quote_s: float = 1.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if poll_s <= 0 or quote_s <= 0:
            raise ValueError("intervals must be positive")
        if (manager.account_id or "").lower() != team_wallet.lower():
            raise ValueError("OMS team account mismatch")
        if manager.dex != client.config.dex or adapter.config != client.config:
            raise ValueError("venue profile mismatch")
        if gateway.manager is not manager or gateway.risk is not risk:
            raise ValueError("gateway must own this OMS and risk instance")
        if live != gateway.live_enabled:
            raise ValueError("requested live mode must equal gateway authorization")
        if live and not verified_unified:
            raise PermissionError("explicitly verify PAS unifiedAccount mode")
        self.client, self.adapter, self.market = client, adapter, market
        self.manager, self.gateway, self.risk, self.journal = manager, gateway, risk, journal
        self.wallet, self.instrument, self.quoter = team_wallet, instrument, quoter
        self.live, self.verified_unified = live, verified_unified
        self.poll_s, self.quote_s = poll_s, quote_s
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.quotes: queue.Queue[list[OrderIntent]] = queue.Queue(maxsize=1)
        self._last_account: VerifiedAccount | None = None
        self._last_account_ms = 0
        self._anchor_ms: int | None = None
        self._baseline_equity: Decimal | None = None

    def _venue_open(self, raw: Any, known: dict[str, Any]) -> tuple[LiveOrder, ...]:
        if not isinstance(raw, list):
            raise ReconciliationError("openOrders missing or malformed")
        result = []
        for row in raw:
            if not isinstance(row, dict) or row.get("coin") != self.adapter.to_venue_instrument(self.instrument):
                raise ReconciliationError("foreign or malformed live order: intervention required")
            cloid = row.get("cloid")
            if not isinstance(cloid, str):
                raise ReconciliationError("open order missing CLOID")
            original = known.get(cloid.lower())
            if original is None:
                raise ReconciliationError("unmanaged resting order: intervention required")
            side = Side.BID if row.get("side") == "B" else Side.ASK if row.get("side") == "A" else None
            if side is None or side.value != original.side:
                raise ReconciliationError("venue/local order side mismatch")
            oid = row.get("oid")
            if not isinstance(oid, int) or isinstance(oid, bool):
                raise ReconciliationError("open order missing valid OID")
            price, remaining = dec(row["limitPx"]), dec(row["sz"])
            if price != original.price or not (Decimal(0) < remaining <= original.size):
                raise ReconciliationError("venue/local price or size mismatch")
            result.append(LiveOrder(
                cloid.lower(), oid, self.instrument, side, price, original.size,
                remaining, OrderState.PARTIALLY_FILLED if remaining < original.size else OrderState.RESTING,
                original.post_only, original.reduce_only,
            ))
        return tuple(result)

    def _read_account(self, known: dict[str, Any]) -> tuple[VerifiedAccount, dict]:
        perps = self.client.query_positions(self.wallet)
        positions = positions_from_perps(perps)
        venue_coin = self.adapter.to_venue_instrument(self.instrument)
        for coin, quantity in positions.items():
            if coin != venue_coin and quantity != 0:
                raise ReconciliationError("position exists outside the single supported market")
        margin = perps.get("marginSummary")
        if not isinstance(margin, dict):
            raise ReconciliationError("missing perp marginSummary")
        equity = dec(margin["accountValue"])
        if equity <= 0:
            raise ReconciliationError("nonpositive or unavailable account equity")
        raw_open = self.client.query_open_orders(self.wallet)
        active = self._venue_open(raw_open, known)
        data = self.client.query_active_asset_data(self.wallet, self.instrument)
        if not isinstance(data, dict) or not isinstance(data.get("maxTradeSzs"), list):
            raise ReconciliationError("unverified maxTradeSzs capacity")
        capacities = data["maxTradeSzs"]
        if len(capacities) != 2:
            raise ReconciliationError("expected independent buy and sell capacities")
        buy, sell = dec(capacities[0]), dec(capacities[1])
        if min(buy, sell) < 0:
            raise ReconciliationError("negative side capacity")
        contexts = self.client.query_meta_and_asset_contexts()
        names = [m["name"] for m in contexts[0]["universe"]]
        if venue_coin not in names:
            raise ReconciliationError("missing selected market context")
        mark = dec(contexts[1][names.index(venue_coin)]["markPx"])
        if mark <= 0:
            raise ReconciliationError("invalid mark")
        return VerifiedAccount({self.instrument: positions.get(venue_coin, Decimal(0))},
                               equity, active, buy, sell, mark), perps

    def _reconcile(self) -> None:
        """OMS thread only: REST/fills/order statuses before making READY true."""
        self.ready_event.clear()
        self.manager.start_recovery("reconciling")
        all_orders = self.journal.load_all_placements().result(timeout=10)
        known = {p.client_order_id.lower(): p for p in all_orders}
        if len(known) != len(all_orders):
            raise ReconciliationError("duplicate historical CLOIDs")
        account, _ = self._read_account(known)
        if self._anchor_ms is None:
            anchor = self.journal.load_anchor().result(timeout=10)
            if anchor is None:
                if all_orders or account.open_orders or any(account.positions.values()):
                    raise ReconciliationError("first boot requires flat account and no prior orders")
                anchor = self.journal.initialize_flat_anchor(
                    self.clock_ms(), account.equity
                ).result(timeout=10)
            self._anchor_ms = anchor
            self._baseline_equity = self.journal.load_baseline_equity().result(timeout=10)
            if self._baseline_equity is None or self._baseline_equity <= 0:
                raise ReconciliationError("missing persisted account equity baseline")

        # Resolve all unresolved journal orders (PREPARED remains potentially sent).
        unresolved = [p for p in all_orders if p.dispatch_state in ("PREPARED", "SENT")]
        observed = {o.client_order_id: o for o in account.open_orders}
        confirmed_closed: set[str] = set()
        oid_to_cloid: dict[int, str] = {o.exchange_order_id: o.client_order_id for o in account.open_orders}
        for p in all_orders:
            if p.dispatch_state == "NOT_SENT":
                continue
            if p.dispatch_state == "CLOSED" and p.exchange_order_id is not None:
                oid_to_cloid[p.exchange_order_id] = p.client_order_id
                if p.client_order_id in observed:
                    raise ReconciliationError("previously closed order is again resting")
                continue
            status, oid = venue_status(self.client.query_order_status(
                self.wallet, client_order_id=p.client_order_id
            ))
            self.journal.remember_order_oid(p.client_order_id, oid).result(timeout=10)
            if oid in oid_to_cloid and oid_to_cloid[oid] != p.client_order_id:
                raise ReconciliationError("OID maps to more than one CLOID")
            oid_to_cloid[oid] = p.client_order_id
            if p.client_order_id in observed and status != "open":
                raise ReconciliationError("orderStatus contradicts openOrders")
            if p.dispatch_state in ("PREPARED", "SENT"):
                original = observed.get(p.client_order_id)
                if original is None:
                    if status not in TERMINAL_VENUE_STATUSES:
                        raise ReconciliationError("journaled order missing without terminal confirmation")
                    confirmed_closed.add(p.client_order_id)
                    self.journal.mark_terminal(p.client_order_id, status).result(timeout=10)
                else:
                    self.manager.live_orders[p.client_order_id] = replace(original)

        fills = self.client.query_fills(self.wallet)
        if not isinstance(fills, list):
            raise ReconciliationError("incomplete or malformed fill response")
        for f in fills:
            if not isinstance(f, dict):
                raise ReconciliationError("invalid fill record")
            ts = f.get("time")
            if not isinstance(ts, int):
                raise ReconciliationError("fill timestamp missing")
            if ts < self._anchor_ms:
                continue
            if f.get("coin") != self.adapter.to_venue_instrument(self.instrument):
                raise ReconciliationError("out-of-scope fill since activation")
            oid, tid = f.get("oid"), f.get("tid")
            if not isinstance(oid, int) or not isinstance(tid, int) or oid not in oid_to_cloid:
                raise ReconciliationError("unmapped fill after activation")
            cloid = oid_to_cloid[oid]
            self.journal.record_fill(
                fill_id=f"{self.manager.dex}:{tid}", client_order_id=cloid,
                instrument=self.instrument, size=dec(f["sz"]), price=dec(f["px"]),
                fee=dec(f["fee"]), event_ts_ms=ts,
            ).result(timeout=10)
        expected = self.journal.load_fill_positions().result(timeout=10)
        # Prior OMS state can remain active even after its journal row is CLOSED.
        for cloid, old in self.manager.live_orders.items():
            if old.active and cloid not in observed:
                if cloid not in known or known[cloid].dispatch_state == "NOT_SENT":
                    raise ReconciliationError("unexpected active local order")
                if cloid not in confirmed_closed:
                    status, _ = venue_status(self.client.query_order_status(
                        self.wallet, client_order_id=cloid
                    ))
                    if status not in TERMINAL_VENUE_STATUSES:
                        raise ReconciliationError("disappeared order not confirmed terminal")
                    confirmed_closed.add(cloid)
                    self.journal.mark_terminal(cloid, status).result(timeout=10)
        if expected.get(self.instrument, Decimal(0)) != account.positions[self.instrument]:
            raise ReconciliationError("venue position != journaled post-anchor fills")
        if not self.manager.resynchronize(
            list(account.open_orders), positions_reconciled=True,
            fills_reconciled=True, confirmed_closed_cloids=frozenset(confirmed_closed),
        ):
            raise ReconciliationError(self.manager.blocked_reason or "OMS state disagreement")
        # Verify stable account/open-order state after the multi-call REST recovery.
        confirmation, _ = self._read_account(known)
        if (confirmation.positions != account.positions or
            confirmation.open_orders != account.open_orders):
            self.manager.start_recovery("account_changed_during_recovery")
            raise ReconciliationError("account changed during recovery; retry")
        self._last_account, self._last_account_ms = confirmation, self.clock_ms()
        self.ready_event.set()

    def _risk_snapshot(self) -> RiskSnapshot:
        account = self._last_account
        book = self.market.current()
        if not self.manager.ready or not self.ready_event.is_set() or account is None or book is None:
            raise ReconciliationError("OMS/account/market not ready")
        # Bound account age to one poll interval + a small scheduling allowance.
        if self.clock_ms() - self._last_account_ms > int(self.poll_s * 1000) + 500:
            raise ReconciliationError("account snapshot stale")
        return RiskSnapshot(
            market=book, now_ms=self.clock_ms(), dex=self.manager.dex,
            expected_dex=self.manager.dex, allowed_instruments=frozenset({self.instrument}),
            positions=account.positions, marks={self.instrument: account.mark},
            fillable_orders=(), account_equity=account.equity,
            baseline_equity=self._baseline_equity,
            margin_mode="unified" if self.verified_unified else None,
            capacity_buy={self.instrument: account.capacity_buy},
            capacity_sell={self.instrument: account.capacity_sell},
            account_id=self.wallet, expected_account_id=self.wallet,
        )

    def _dispatch(self, action: Any) -> None:
        if isinstance(action, PlaceAction):
            receipt = self.journal.prepare_place(action).result(timeout=10)
            try:
                outcome = self.gateway.dispatch(action, self._risk_snapshot)
            except BaseException:
                self.manager.start_recovery("ambiguous_execution")
                raise
            oid = self.manager.live_orders[action.intent.client_order_id].exchange_order_id
            self.journal.record_place_outcome(
                receipt, sent=outcome.sent, reason=outcome.reason,
                exchange_order_id=oid,
            ).result(timeout=10)
        elif isinstance(action, CancelAction):
            # Emergency cancels must not wait for a disk write.
            outcome = self.gateway.dispatch(action, self._risk_snapshot)
            self.journal.record_cancel(
                action, sent=outcome.sent, reason=outcome.reason
            ).result(timeout=10)
        else:
            raise TypeError("unknown action")

    def _quoter_loop(self) -> None:
        try:
            while not self.stop_event.wait(self.quote_s):
                if not self.ready_event.is_set():
                    continue
                book = self.market.current()
                desired = self.quoter.desired(book)
                if not desired:
                    continue
                latest = []
                for intent in desired:
                    request = self.adapter.to_venue_order(intent, self.gateway.market_spec)
                    latest.append(replace(intent, price=dec(request.price), size=dec(request.size)))
                if self.quotes.full():
                    try:
                        self.quotes.get_nowait()
                    except queue.Empty:
                        pass
                self.quotes.put_nowait(latest)
        except BaseException:
            LOG.exception("quoter worker failed")
            self.ready_event.clear()
            self.stop_event.set()

    def _cancel_known(self) -> None:
        """Best-effort risk-reducing cancellation, never proof of closure."""
        for action in self.manager.request_cancel_all():
            try:
                self._dispatch(action)
            except Exception:
                LOG.exception("emergency cancel uncertain; manual venue check required")

    def run(self) -> None:
        """Call from the OMS owner/main thread; errors pause rather than restart blind."""
        worker = threading.Thread(target=self._quoter_loop, name="pas-quoter", daemon=True)
        self.market.start()
        worker.start()
        try:
            while not self.stop_event.is_set():
                try:
                    self._reconcile()
                    if self.live and not self.market.ready:
                        self.ready_event.clear()
                        self._cancel_known()
                        self.manager.start_recovery("market_data_stale")
                        raise ReconciliationError("market data unavailable; stale quotes cancelled")
                    if self.live:
                        try:
                            intents = self.quotes.get(timeout=self.quote_s)
                        except queue.Empty:
                            intents = []
                        if intents:
                            for action in self.manager.reconcile(intents):
                                self._dispatch(action)
                    else:
                        book = self.market.current()
                        if book is not None and book.is_valid:
                            LOG.info("DRY_RUN: desired quotes %s", self.quoter.desired(book))
                    self.stop_event.wait(self.poll_s)
                except (Exception, KeyboardInterrupt):
                    self.ready_event.clear()
                    self.manager.start_recovery("runtime_failure")
                    LOG.exception("MVP-0 fail closed; new orders disabled")
                    if self.live:
                        self._cancel_known()
                    # Halt rather than endlessly attempting trades on uncertain state.
                    break
        finally:
            if self.live:
                self._cancel_known()
            self.ready_event.clear()
            self.stop_event.set()
            worker.join(timeout=3)
            self.market.stop()
            self.client.close_stream()
