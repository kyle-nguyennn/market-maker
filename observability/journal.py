"""Bounded, async diagnostic journal.

This is *not* a transactional order ledger: enqueue success != disk durability.
Unattended order dispatch still requires its own persistent order identity log.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from queue import Full, Queue
from threading import Lock, Thread
from typing import Callable

from observability.events import OperationalEvent


class JsonlSink:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def __call__(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as output:
            output.write(line + "\n")


@dataclass(frozen=True)
class JournalStats:
    accepted: int
    written: int
    dropped: int
    failed: bool
    closed: bool


class AsyncEventJournal:
    """Never perform filesystem/network I/O on publish().

    An overflow or persistent sink failure is visible via stats, and callers
    may explicitly pause trading under a stricter operational policy.
    """

    _STOP = object()

    def __init__(self, sink: Callable[[str], None], *, capacity: int = 2048) -> None:
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be positive")
        self._sink = sink
        self._queue: Queue[str | object] = Queue(maxsize=capacity)
        self._lock = Lock()
        self._accepted = 0
        self._written = 0
        self._dropped = 0
        self._failed = False
        self._closed = False
        self._worker = Thread(target=self._run, name="ops-event-journal", daemon=True)
        self._worker.start()

    def publish(self, event: OperationalEvent) -> bool:
        line = event.to_json()  # Validate/redact in caller; never enqueue invalid data.
        with self._lock:
            if self._closed or self._failed:
                self._dropped += 1
                return False
            try:
                self._queue.put_nowait(line)
            except Full:
                self._dropped += 1
                return False
            self._accepted += 1
            return True

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                with self._lock:
                    failed = self._failed
                if not failed:
                    try:
                        self._sink(item)
                    except Exception:
                        # Never expose exception strings: could carry credentials.
                        with self._lock:
                            self._failed = True
                            self._dropped += 1
                    else:
                        with self._lock:
                            self._written += 1
                else:
                    with self._lock:
                        self._dropped += 1
            finally:
                self._queue.task_done()

    def stats(self) -> JournalStats:
        with self._lock:
            return JournalStats(
                self._accepted, self._written, self._dropped,
                self._failed, self._closed,
            )

    def drain(self) -> None:
        """Wait for diagnostic queue delivery; call outside the trading hot path."""
        self._queue.join()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(self._STOP)  # Shutdown may wait for capacity.
        self._worker.join()
