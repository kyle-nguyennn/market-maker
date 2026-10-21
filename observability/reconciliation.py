"""Authoritative scoped account/order verification on top of OrderManager.

No venue I/O or automatic ledger mutations. The adapter supplies two
complete, comparable scoped snapshots and confirms historical coverage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Mapping

from execution.order_manager import LiveOrder, OrderManager
from observability.events import EventScope


@dataclass(frozen=True)
class LedgerSnapshot:
    scope: EventScope
    positions: Mapping[str, Decimal]
    cash: Decimal
    fill_ids: frozenset[str]
    # The adapter must prove the same fill-history interval was fetched on both sides.
    coverage_id: str


@dataclass(frozen=True)
class VenueSnapshot(LedgerSnapshot):
    observed_at: datetime
    open_orders: tuple[LiveOrder, ...] = ()
    confirmed_closed_cloids: frozenset[str] = field(default_factory=frozenset)
    complete: bool = False


@dataclass(frozen=True)
class ReconcileResult:
    verified: bool
    reason: str


class AccountReconciler:
    def __init__(self, manager: OrderManager, scope: EventScope, *,
                 max_snapshot_age: timedelta = timedelta(seconds=30)) -> None:
        if max_snapshot_age <= timedelta(0):
            raise ValueError("snapshot age must be positive")
        self.manager = manager
        self.scope = scope
        self.max_snapshot_age = max_snapshot_age

    @staticmethod
    def _valid_ledger(ledger: LedgerSnapshot) -> bool:
        if not isinstance(ledger.cash, Decimal) or not ledger.cash.is_finite():
            return False
        if not ledger.coverage_id or not isinstance(ledger.fill_ids, frozenset):
            return False
        if any(not isinstance(k, str) or not k for k in ledger.fill_ids):
            return False
        return all(
            isinstance(symbol, str) and bool(symbol)
            and isinstance(value, Decimal) and value.is_finite()
            for symbol, value in ledger.positions.items()
        )

    def verify(self, local: LedgerSnapshot, venue: VenueSnapshot, *,
               now: datetime | None = None) -> ReconcileResult:
        self.manager.start_recovery("account_reconciliation_in_progress")
        now = now or datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() is None:
            return ReconcileResult(False, "unverified_clock")
        if not local.scope.same_account(self.scope) or not venue.scope.same_account(self.scope):
            return ReconcileResult(False, "scope_mismatch")
        if (self.manager.account_id is not None
                and self.manager.account_id.casefold() != self.scope.account_id.casefold()):
            return ReconcileResult(False, "order_manager_account_mismatch")
        if self.manager.dex is not None and self.manager.dex != self.scope.environment:
            return ReconcileResult(False, "order_manager_environment_mismatch")
        if not venue.complete:
            return ReconcileResult(False, "incomplete_venue_snapshot")
        if venue.observed_at.tzinfo is None or venue.observed_at.utcoffset() is None:
            return ReconcileResult(False, "missing_timestamp")
        age = now - venue.observed_at
        if age > self.max_snapshot_age or age < timedelta(seconds=-1):
            return ReconcileResult(False, "stale_or_future_snapshot")
        if not self._valid_ledger(local) or not self._valid_ledger(venue):
            return ReconcileResult(False, "invalid_ledger")
        if local.coverage_id != venue.coverage_id:
            return ReconcileResult(False, "unmatched_fill_coverage")
        if local.fill_ids != venue.fill_ids:
            return ReconcileResult(False, "unreconciled_fills")
        if local.positions != venue.positions:
            return ReconcileResult(False, "position_mismatch")
        if local.cash != venue.cash:
            return ReconcileResult(False, "cash_mismatch")
        if not self.manager.resynchronize(
            list(venue.open_orders),
            positions_reconciled=True,
            fills_reconciled=True,
            confirmed_closed_cloids=venue.confirmed_closed_cloids,
        ):
            return ReconcileResult(False, self.manager.blocked_reason or "order_mismatch")
        return ReconcileResult(True, "reconciled")
