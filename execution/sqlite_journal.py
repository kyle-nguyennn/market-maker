"""Single-writer SQLite WAL execution journal. Never called from market-data locks.

Orders are durably PREPARED before an execution dispatcher may send them.
A PREPARED order is *ambiguous* on recovery: it might have been sent before
a crash. The exchange, not the database, is the source of live order truth.
"""
from __future__ import annotations

import queue
import re
import sqlite3
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, TypeVar
from uuid import uuid4

from model.order import CancelAction, PlaceAction

T = TypeVar("T")
_CLOID = re.compile(r"0x[0-9a-fA-F]{32}\Z")
_SCHEMA_VERSION = 1


class JournalUnavailable(RuntimeError):
    """A write cannot be trusted; do not submit a new exchange order."""


class JournalConflict(RuntimeError):
    """Duplicate or inconsistent action/fill identity."""


@dataclass(frozen=True)
class JournalReceipt:
    action_id: int
    action_key: str
    client_order_id: str


@dataclass(frozen=True)
class StoredPlacement:
    action_id: int
    client_order_id: str
    instrument: str
    side: str
    price: Decimal
    size: Decimal
    post_only: bool
    reduce_only: bool
    dispatch_state: str
    exchange_order_id: int | None


@dataclass(frozen=True)
class RecoverySnapshot:
    dex: str
    account_id: str
    unresolved: tuple[StoredPlacement, ...]
    fill_count: int
    kill_switch: bool
    kill_reason: str | None


@dataclass(frozen=True)
class _Request:
    operation: Callable[[sqlite3.Connection], object]
    future: Future


class SQLiteWALJournal:
    """Bounded nonblocking submission API backed by one dedicated DB thread.

    Construct/close on a control thread. Runtime methods return concurrent
    futures; never wait for those futures in a WebSocket callback.
    The local DB must be on a persistent disk, not ephemeral container storage.
    """

    def __init__(
        self, path: str | Path, *, dex: str, account_id: str,
        max_pending: int = 512, startup_timeout_s: float = 10.0,
    ) -> None:
        if not dex or ":" in dex:
            raise ValueError("invalid DEX")
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", account_id):
            raise ValueError("invalid team wallet address")
        if not isinstance(max_pending, int) or max_pending <= 0:
            raise ValueError("max_pending must be positive")
        self.path = Path(path)
        if str(path) == ":memory:":
            raise ValueError("execution journal must be a persistent file")
        self.dex = dex
        self.account_id = account_id.lower()
        self._queue: queue.Queue[_Request | None] = queue.Queue(maxsize=max_pending)
        self._lock = threading.Lock()
        self._closed = False
        self._fatal: BaseException | None = None
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sqlite-execution-journal", daemon=True)
        self._thread.start()
        if not self._started.wait(startup_timeout_s):
            self._fatal = JournalUnavailable("SQLite startup timed out")
            self._closed = True
            raise JournalUnavailable("SQLite startup timed out")
        if self._fatal is not None:
            self._closed = True
            raise JournalUnavailable("SQLite initialization failed") from self._fatal

    def _initialize(self, conn: sqlite3.Connection) -> None:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode.lower() != "wal":
            raise JournalUnavailable("SQLite WAL could not be enabled")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS journal_identity (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    schema_version INTEGER NOT NULL,
                    dex TEXT NOT NULL,
                    account_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS journal_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL CHECK(kind IN ('PLACE', 'CANCEL')),
                    client_order_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    side TEXT,
                    price TEXT,
                    size TEXT,
                    post_only INTEGER,
                    reduce_only INTEGER,
                    dispatch_state TEXT NOT NULL,
                    outcome_reason TEXT,
                    exchange_order_id INTEGER,
                    created_ns INTEGER NOT NULL,
                    updated_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS journal_fills (
                    fill_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    size TEXT NOT NULL,
                    price TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    event_ts_ms INTEGER NOT NULL,
                    received_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS journal_safety (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    kill_switch INTEGER NOT NULL,
                    kill_reason TEXT,
                    changed_ns INTEGER NOT NULL
                );
            """)
            existing = conn.execute(
                "SELECT schema_version,dex,account_id FROM journal_identity WHERE id=1"
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO journal_identity VALUES(1,?,?,?)",
                    (_SCHEMA_VERSION, self.dex, self.account_id),
                )
            elif existing != (_SCHEMA_VERSION, self.dex, self.account_id):
                raise JournalConflict(
                    "journal schema / DEX / team wallet mismatch; refusing to reuse database"
                )
            conn.execute(
                "INSERT OR IGNORE INTO journal_safety VALUES(1,0,NULL,?)",
                (time.time_ns(),),
            )

    def _run(self) -> None:
        conn: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=3.0)
            self._initialize(conn)
            self._started.set()
            while True:
                item = self._queue.get()
                if item is None:
                    break
                if item.future.cancelled():
                    continue
                try:
                    with conn:
                        value = item.operation(conn)
                except (JournalConflict, ValueError) as exc:
                    item.future.set_exception(exc)
                except Exception as exc:
                    item.future.set_exception(JournalUnavailable("SQLite operation failed"))
                    raise exc
                else:
                    item.future.set_result(value)
        except BaseException as exc:
            with self._lock:
                self._fatal = exc
            self._started.set()
            while True:
                try:
                    pending = self._queue.get_nowait()
                except queue.Empty:
                    break
                if pending is not None and not pending.future.done():
                    pending.future.set_exception(JournalUnavailable("journal worker stopped"))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _submit(self, operation: Callable[[sqlite3.Connection], T]) -> Future[T]:
        future: Future[T] = Future()
        with self._lock:
            if self._closed or self._fatal is not None or not self._thread.is_alive():
                raise JournalUnavailable("SQLite journal not available")
            try:
                self._queue.put_nowait(_Request(operation, future))
            except queue.Full as exc:
                raise JournalUnavailable("SQLite journal queue full") from exc
        return future

    @staticmethod
    def _number(value: Decimal, *, positive: bool = False) -> str:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError("journal requires finite Decimal")
        if positive and value <= 0:
            raise ValueError("journal requires positive Decimal")
        return str(value)

    def prepare_place(self, action: PlaceAction) -> Future[JournalReceipt]:
        """Commit exactly one intent per CLOID; duplicate preparation fails closed."""
        intent = action.intent
        cloid = intent.client_order_id
        if cloid is None or _CLOID.fullmatch(cloid) is None:
            raise ValueError("a 128-bit client order ID must be allocated first")
        if not intent.instrument or ":" in intent.instrument:
            raise ValueError("journal accepts unqualified instruments only")
        price, size = self._number(intent.price, positive=True), self._number(intent.size, positive=True)
        key = "place:" + cloid.lower()
        def write(conn: sqlite3.Connection) -> JournalReceipt:
            now = time.time_ns()
            try:
                cursor = conn.execute(
                    """INSERT INTO journal_actions
                    (action_key,kind,client_order_id,instrument,side,price,size,
                     post_only,reduce_only,dispatch_state,created_ns,updated_ns)
                    VALUES(?,'PLACE',?,?,?,?,?,?,?,'PREPARED',?,?)""",
                    (key, cloid.lower(), intent.instrument, intent.side.value, price, size,
                     int(intent.post_only), int(intent.reduce_only), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise JournalConflict("CLOID already journaled; never resend blindly") from exc
            return JournalReceipt(cursor.lastrowid, key, cloid.lower())
        return self._submit(write)

    def record_place_outcome(
        self, receipt: JournalReceipt, *, sent: bool, reason: str,
        exchange_order_id: int | None = None,
    ) -> Future[None]:
        """Records transport outcome, not proof that an order is terminal."""
        if not reason:
            raise ValueError("outcome reason required")
        def write(conn: sqlite3.Connection) -> None:
            updated = conn.execute(
                """UPDATE journal_actions SET dispatch_state=?, outcome_reason=?,
                   exchange_order_id=?,updated_ns=?
                   WHERE id=? AND action_key=? AND client_order_id=?
                     AND kind='PLACE' AND dispatch_state='PREPARED'""",
                ("SENT" if sent else "NOT_SENT", reason, exchange_order_id,
                 time.time_ns(), receipt.action_id, receipt.action_key, receipt.client_order_id),
            ).rowcount
            if updated != 1:
                raise JournalConflict("missing or already finalized journal receipt")
        return self._submit(write)

    def record_cancel(
        self, action: CancelAction, *, sent: bool, reason: str,
    ) -> Future[int]:
        """Best-effort post-dispatch audit; NEVER block emergency cancellation."""
        if not action.client_order_id or not action.instrument or not reason:
            raise ValueError("invalid cancellation audit")
        def write(conn: sqlite3.Connection) -> int:
            now = time.time_ns()
            row = conn.execute(
                """INSERT INTO journal_actions
                (action_key,kind,client_order_id,instrument,dispatch_state,
                 outcome_reason,exchange_order_id,created_ns,updated_ns)
                 VALUES(?,'CANCEL',?,?,?,?,?,?,?)""",
                ("cancel:" + uuid4().hex, action.client_order_id.lower(),
                 action.instrument, "SENT" if sent else "NOT_SENT", reason,
                 action.exchange_order_id, now, now),
            )
            return row.lastrowid
        return self._submit(write)

    def record_fill(
        self, *, fill_id: str, client_order_id: str, instrument: str,
        size: Decimal, price: Decimal, fee: Decimal, event_ts_ms: int,
    ) -> Future[bool]:
        """Durably deduplicate normalized venue fills. True means newly stored."""
        if not fill_id or not client_order_id or not instrument or ":" in instrument:
            raise ValueError("invalid fill identity")
        values = (client_order_id.lower(), instrument, self._number(size, positive=True),
                  self._number(price, positive=True), self._number(fee), event_ts_ms)
        if not isinstance(event_ts_ms, int) or event_ts_ms < 0:
            raise ValueError("invalid fill timestamp")
        def write(conn: sqlite3.Connection) -> bool:
            prior = conn.execute(
                """SELECT client_order_id,instrument,size,price,fee,event_ts_ms
                FROM journal_fills WHERE fill_id=?""", (fill_id,),
            ).fetchone()
            if prior is not None:
                if prior != values:
                    raise JournalConflict("same fill ID with conflicting payload")
                return False
            conn.execute(
                """INSERT INTO journal_fills
                (fill_id,client_order_id,instrument,size,price,fee,event_ts_ms,received_ns)
                VALUES(?,?,?,?,?,?,?,?)""",
                (fill_id, *values, time.time_ns()),
            )
            return True
        return self._submit(write)

    def latch_kill_switch(self, reason: str) -> Future[None]:
        if not reason:
            raise ValueError("kill switch reason must be nonempty")
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """UPDATE journal_safety SET kill_switch=1,
                   kill_reason=COALESCE(kill_reason,?),changed_ns=? WHERE id=1""",
                (reason, time.time_ns()),
            )
        return self._submit(write)

    def load_recovery(self) -> Future[RecoverySnapshot]:
        """Read durable candidates. EVERY potentially sent order needs REST+fill sync."""
        def read(conn: sqlite3.Connection) -> RecoverySnapshot:
            rows = conn.execute(
                """SELECT id,client_order_id,instrument,side,price,size,post_only,
                   reduce_only,dispatch_state,exchange_order_id
                   FROM journal_actions WHERE kind='PLACE'
                   AND dispatch_state IN ('PREPARED','SENT') ORDER BY id"""
            ).fetchall()
            orders = tuple(
                StoredPlacement(r[0], r[1], r[2], r[3], Decimal(r[4]), Decimal(r[5]),
                                bool(r[6]), bool(r[7]), r[8], r[9])
                for r in rows
            )
            count = conn.execute("SELECT COUNT(*) FROM journal_fills").fetchone()[0]
            kill, reason = conn.execute(
                "SELECT kill_switch,kill_reason FROM journal_safety WHERE id=1"
            ).fetchone()
            return RecoverySnapshot(
                self.dex, self.account_id, orders, count, bool(kill), reason
            )
        return self._submit(read)

    def close(self) -> None:
        """Drain accepted writes. Call from process shutdown, not a market callback."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        # Queue may be full; close intentionally waits for accepted operations.
        if self._thread.is_alive():
            self._queue.put(None)
            self._thread.join()

    def __enter__(self) -> "SQLiteWALJournal":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
