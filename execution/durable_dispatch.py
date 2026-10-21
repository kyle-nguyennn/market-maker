"""Off-hot-path execution orchestration with a SQLite durability barrier.

The quoter must never call the SDK directly. This class composes the existing
risk-checked SafeExecutionGateway with an execution journal. This is a bounded
single-process component, NOT a live trading daemon or a startup resynchronizer.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import BoundedSemaphore, RLock
from typing import Callable

from execution.safe_gateway import GatewayOutcome, SafeExecutionGateway
from execution.sqlite_journal import JournalUnavailable, SQLiteWALJournal
from model.order import CancelAction, OrderAction, PlaceAction
from risk.risk_manager import RiskSnapshot


class DurableExecutionDispatcher:
    """Async to caller, serialized at the SDK boundary, fail-closed for places.

    The producer must serialize external OrderManager modifications with this
    gateway or use a single owner/event-loop handoff; the manager itself is NOT
    thread-safe. The SDK boundary is guarded against two internal executors
    dispatching concurrently. A cancellation does not wait for disk.
    """

    def __init__(
        self, *, gateway: SafeExecutionGateway, journal: SQLiteWALJournal,
        max_pending_places: int = 128, max_pending_cancels: int = 32,
    ) -> None:
        manager = gateway.manager
        if (manager.dex != journal.dex or not manager.account_id or
                manager.account_id.lower() != journal.account_id):
            raise ValueError("journal and execution gateway DEX/account mismatch")
        if max_pending_places <= 0 or max_pending_cancels <= 0:
            raise ValueError("pending action capacities must be positive")
        self.gateway = gateway
        self.journal = journal
        self._places = ThreadPoolExecutor(max_workers=1, thread_name_prefix="journaled-place")
        self._cancels = ThreadPoolExecutor(max_workers=1, thread_name_prefix="urgent-cancel")
        self._place_capacity = BoundedSemaphore(max_pending_places)
        self._cancel_capacity = BoundedSemaphore(max_pending_cancels)
        self._gateway_lock = RLock()
        self._closed = False

    def submit(
        self, action: OrderAction,
        snapshot_provider: Callable[[], RiskSnapshot],
    ) -> Future[GatewayOutcome]:
        """Schedule work without doing filesystem or SDK I/O on the caller."""
        if self._closed:
            raise JournalUnavailable("execution dispatcher is closed")
        if isinstance(action, PlaceAction):
            capacity, pool, worker = self._place_capacity, self._places, self._place
        elif isinstance(action, CancelAction):
            capacity, pool, worker = self._cancel_capacity, self._cancels, self._cancel
        else:
            raise TypeError("unsupported order action")
        if not capacity.acquire(blocking=False):
            if isinstance(action, PlaceAction):
                self.gateway.manager.start_recovery("execution_dispatch_queue_full")
            raise JournalUnavailable("execution action queue saturated")
        try:
            future = pool.submit(worker, action, snapshot_provider)
        except Exception:
            capacity.release()
            raise
        future.add_done_callback(lambda _: capacity.release())
        return future

    def submit_many(
        self, actions: list[OrderAction],
        snapshot_provider: Callable[[], RiskSnapshot],
    ) -> list[Future[GatewayOutcome]]:
        """Prioritize cancelling before accepting new place work."""
        cancels = [a for a in actions if isinstance(a, CancelAction)]
        places = [a for a in actions if isinstance(a, PlaceAction)]
        if len(cancels) + len(places) != len(actions):
            raise TypeError("unsupported order action")
        return [self.submit(a, snapshot_provider) for a in cancels + places]

    def _place(
        self, action: PlaceAction, snapshot_provider: Callable[[], RiskSnapshot],
    ) -> GatewayOutcome:
        try:
            # Future resolves ONLY after durable SQLite transaction commit.
            receipt = self.journal.prepare_place(action).result()
        except Exception as exc:
            self.gateway.manager.start_recovery("order_journal_prepare_failed")
            raise JournalUnavailable("new order NOT SENT: durable prepare failed") from exc
        try:
            # Fresh risk/account/market checks run AFTER the disk barrier.
            with self._gateway_lock:
                outcome = self.gateway.dispatch(action, snapshot_provider)
        except Exception:
            # The caller cannot distinguish an SDK timeout from a local failure.
            # Leave PREPARED for REST + fills reconciliation.
            self.gateway.manager.start_recovery("order_dispatch_uncertain")
            raise
        try:
            # ACK and rejection events are also journaled, but a crash before
            # this write is conservatively recovered as PREPARED/unknown.
            self.journal.record_place_outcome(
                receipt, sent=outcome.sent, reason=outcome.reason
            ).result()
        except Exception as exc:
            self.gateway.manager.start_recovery("order_journal_outcome_failed")
            raise JournalUnavailable(
                "order may have been sent: outcome journal failed; reconcile venue"
            ) from exc
        return outcome

    def _cancel(
        self, action: CancelAction, snapshot_provider: Callable[[], RiskSnapshot],
    ) -> GatewayOutcome:
        # Never wait for a disk barrier before an emergency cancellation.
        with self._gateway_lock:
            outcome = self.gateway.dispatch(action, snapshot_provider)
        try:
            self.journal.record_cancel(
                action, sent=outcome.sent, reason=outcome.reason
            ).result()
        except Exception as exc:
            self.gateway.manager.start_recovery("cancel_audit_failed")
            raise JournalUnavailable(
                "cancel attempted but audit unavailable; verify venue state"
            ) from exc
        return outcome

    def close(self) -> None:
        """Finish accepted execution tasks before shutting down the journal."""
        self._closed = True
        self._cancels.shutdown(wait=True)
        self._places.shutdown(wait=True)

    def __enter__(self) -> "DurableExecutionDispatcher":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
