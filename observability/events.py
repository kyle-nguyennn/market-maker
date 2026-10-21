"""Structured, venue-scoped operational events with exact Decimal encoding."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from json import dumps
from math import isfinite
from typing import Any, Mapping
from uuid import uuid4


SENSITIVE_KEYS = (
    "password", "secret", "token", "private", "api_key",
    "authorization", "credential", "signature", "seed_phrase",
)


@dataclass(frozen=True)
class EventScope:
    venue: str
    environment: str
    account_id: str
    tournament_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("venue", "environment", "account_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"invalid scope {field_name}")
        if self.tournament_id is not None and (
            not isinstance(self.tournament_id, str) or not self.tournament_id
        ):
            raise ValueError("invalid tournament_id")

    def same_account(self, other: "EventScope") -> bool:
        return (
            isinstance(other, EventScope)
            and self.venue == other.venue
            and self.environment == other.environment
            and self.account_id.casefold() == other.account_id.casefold()
            and self.tournament_id == other.tournament_id
        )

    def sanitized(self) -> dict[str, str | None]:
        return {
            "venue": self.venue,
            "environment": self.environment,
            "account_hash": sha256(self.account_id.casefold().encode()).hexdigest()[:16],
            "tournament_id": self.tournament_id,
        }


def _safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite Decimal in event")
        return format(value, "f")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("nonfinite float in event")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive event timestamp")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return {
            str(k): ("<redacted>" if any(s in str(k).lower() for s in SENSITIVE_KEYS)
                     else _safe(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    raise TypeError(f"unsupported event payload type: {type(value).__name__}")


@dataclass(frozen=True)
class OperationalEvent:
    scope: EventScope
    event_type: str
    correlation_id: str
    payload: Mapping[str, Any]
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_id: str = field(default_factory=lambda: uuid4().hex)
    config_hash: str | None = None
    deployment_sha: str | None = None

    def to_record(self) -> dict[str, Any]:
        if not isinstance(self.scope, EventScope):
            raise ValueError("missing event scope")
        if not self.event_type or not self.correlation_id or not self.event_id:
            raise ValueError("event_type, event_id and correlation_id are mandatory")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("event timestamp must be timezone aware")
        return {
            "schema_version": 1,
            "event_id": self.event_id,
            "correlation_id": self.correlation_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "scope": self.scope.sanitized(),
            "config_hash": self.config_hash,
            "deployment_sha": self.deployment_sha,
            "payload": _safe(self.payload),
        }

    def to_json(self) -> str:
        return dumps(self.to_record(), separators=(",", ":"), sort_keys=True, allow_nan=False)
