"""Independent dead-man file contract for an external scheduled watchdog.

Writer is atomic; an external process can observe missing/stale heartbeats even
if the trading process has crashed. It cannot verify or cancel exchange orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from json import dumps, loads
from pathlib import Path
from tempfile import NamedTemporaryFile
from os import replace, unlink

from observability.events import EventScope


@dataclass(frozen=True)
class WatchdogResult:
    healthy: bool
    reason: str


def write_heartbeat(
    path: str | Path, scope: EventScope, *, now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("heartbeat time must be timezone aware")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = dumps({
        "schema_version": 1,
        "scope": scope.sanitized(),
        "at_utc": now.astimezone(UTC).isoformat(),
    })
    temp_path: str | None = None
    try:
        with NamedTemporaryFile("w", dir=destination.parent, delete=False,
                                encoding="utf-8") as temp:
            temp_path = temp.name
            temp.write(payload)
            temp.flush()
        replace(temp_path, destination)
    finally:
        if temp_path is not None and Path(temp_path).exists():
            unlink(temp_path)


def inspect_heartbeat(
    path: str | Path,
    scope: EventScope,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(seconds=30),
) -> WatchdogResult:
    if max_age <= timedelta(0):
        raise ValueError("max_age must be positive")
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        return WatchdogResult(False, "unverified_clock")
    try:
        data = loads(Path(path).read_text(encoding="utf-8"))
        if data["schema_version"] != 1 or data["scope"] != scope.sanitized():
            return WatchdogResult(False, "scope_or_schema_mismatch")
        at = datetime.fromisoformat(data["at_utc"])
        if at.tzinfo is None or at.utcoffset() is None:
            return WatchdogResult(False, "invalid_heartbeat_timestamp")
        age = now - at
        if age > max_age or age < timedelta(seconds=-1):
            return WatchdogResult(False, "stale_or_future_heartbeat")
        return WatchdogResult(True, "healthy")
    except FileNotFoundError:
        return WatchdogResult(False, "missing_heartbeat")
    except (OSError, ValueError, KeyError, TypeError):
        return WatchdogResult(False, "invalid_heartbeat")
