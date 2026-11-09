"""Synchronous AlphArena paper-trade API client. No automatic retries."""
from __future__ import annotations

import math
import os
import re
from http.client import HTTPException
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .serialization import decode_json, encode_json

BASE_URL = "https://alpharena.io/api/v1"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class AlphArenaError(RuntimeError):
    """Sanitized failure; mutation outcome may need reconciliation."""

    def __init__(self, code: str, *, status: int | None = None,
                 outcome_unknown: bool = False, retry_after: str | None = None):
        self.code = code
        self.status = status
        self.outcome_unknown = outcome_unknown
        self.retry_after = retry_after
        super().__init__(f"AlphArena {code} (HTTP {status})")


class AuthenticationError(AlphArenaError):
    pass


class RateLimitError(AlphArenaError):
    pass


class TransportError(AlphArenaError):
    pass


class ProtocolError(AlphArenaError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a bearer credential to a redirected host.
        return None


def _send(request: Request, timeout: float) -> HttpResponse:
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.read(), dict(response.headers))
    except HTTPError as exc:
        with exc:
            return HttpResponse(exc.code, exc.read(), dict(exc.headers))


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid agent or trade identifier")
    return value


class AlphArenaClient:
    """API keys are supplied explicitly or via from_environment().

    mode='sandbox' requires aa_test_; mode='leaderboard' requires aa_live_.
    Both are paper trading. Constructors perform no network requests. All
    results are venue dictionaries: fractional JSON numbers become Decimal,
    integral numbers remain exact int. Domain normalization is caller-owned.
    Transport injection is for offline tests; it receives the bearer key.
    """

    def __init__(self, api_key: str, *, mode: str = "sandbox", timeout: float = 10,
                 transport: Callable[[Request, float], HttpResponse] | None = None):
        prefix = {"sandbox": "aa_test_", "leaderboard": "aa_live_"}.get(mode)
        if prefix is None:
            raise ValueError("mode must be sandbox or leaderboard")
        if (not isinstance(api_key, str) or not api_key.startswith(prefix)
                or len(api_key) <= len(prefix) or any(c.isspace() for c in api_key)
                or not api_key.isascii() or not api_key.isprintable()):
            raise ValueError("API key is missing, invalid, or does not match mode")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self._api_key = api_key
        self.mode = mode
        self.timeout = timeout
        self._transport = transport or _send
        self.last_rate_limit_headers: dict[str, str] = {}

    @classmethod
    def from_environment(cls, *, mode: str = "sandbox", **kwargs: Any) -> AlphArenaClient:
        name = "ALPHARENA_SANDBOX_API_KEY" if mode == "sandbox" else "ALPHARENA_API_KEY"
        return cls(os.environ.get(name, ""), mode=mode, **kwargs)

    def _request(self, method: str, path: str, *, payload: dict | None = None,
                 query: dict | None = None, authenticated: bool = True) -> dict[str, Any]:
        url = BASE_URL + path
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self._api_key
        body = encode_json(payload) if payload is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        mutation = method != "GET"
        self.last_rate_limit_headers = {}
        try:
            response = self._transport(request, self.timeout)
        except (URLError, OSError, HTTPException):
            raise TransportError("TRANSPORT_ERROR", outcome_unknown=mutation) from None
        response_headers = {k.lower(): v for k, v in response.headers.items()}
        self.last_rate_limit_headers = {
            k: v for k, v in response_headers.items() if k.startswith("x-ratelimit-")
        }
        try:
            result = decode_json(response.body)
        except (ValueError, UnicodeError):
            result = None
        if not 200 <= response.status < 300 or (result is not None and "error" in result):
            error = result.get("error") if result else None
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
                code = "HTTP_ERROR"
            error_type = (AuthenticationError if response.status in (401, 403)
                          else RateLimitError if response.status == 429 else AlphArenaError)
            # Do not include raw response bodies/messages (may echo credentials).
            raise error_type(code, status=response.status,
                             outcome_unknown=mutation and (response.status >= 500 or
                                                          200 <= response.status < 300),
                             retry_after=response_headers.get("retry-after"))
        if result is None:
            raise ProtocolError("INVALID_RESPONSE", status=response.status,
                                outcome_unknown=mutation)
        return result

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/agents/{_identifier(agent_id)}", authenticated=False)

    def get_portfolio(self, agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/agents/{_identifier(agent_id)}/portfolio", authenticated=False)

    def get_agent_trades(self, agent_id: str, *, page: int = 1,
                         limit: int = 50) -> dict[str, Any]:
        """Public, paginated trades for an agent (not the authenticated own-trades feed)."""
        if (type(page) is not int or page < 1 or type(limit) is not int
                or not 1 <= limit <= 100):
            raise ValueError("invalid pagination")
        return self._request("GET", f"/agents/{_identifier(agent_id)}/trades",
                             query={"page": page, "limit": limit}, authenticated=False)

    def get_leaderboard(self) -> dict[str, Any]:
        """Public leaderboard snapshot; separate from own-trade reconciliation."""
        return self._request("GET", "/leaderboard", authenticated=False)

    def get_strategies(self) -> dict[str, Any]:
        """Public aggregate strategy statistics, not a historical market-data feed."""
        return self._request("GET", "/strategies", authenticated=False)

    def get_pnl(self, agent_id: str, *, period: str = "all") -> dict[str, Any]:
        return self._request("GET", f"/agents/{_identifier(agent_id)}/pnl",
                             query={"period": period}, authenticated=False)

    def list_trades(self, *, status: str = "all", page: int = 1,
                    limit: int = 50) -> dict[str, Any]:
        if status not in {"all", "open", "closed"}:
            raise ValueError("invalid trade status")
        if (type(page) is not int or page < 1 or type(limit) is not int
                or not 1 <= limit <= 100):
            raise ValueError("invalid pagination")
        return self._request("GET", "/trades", query={"status": status, "page": page, "limit": limit})

    def get_trade(self, trade_id: str) -> dict[str, Any]:
        return self._request("GET", f"/trades/{_identifier(trade_id)}")

    def open_trade(self, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        """Submit once. Caller must persist and reuse the key for the same intent.

        entry_price is a reference for stocks, NOT a resting limit order.
        Option schemas and account-level limits are validated by the venue.
        """
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("a stable idempotency_key is required")
        if "idempotency_key" in payload and payload["idempotency_key"] != idempotency_key:
            raise ValueError("conflicting idempotency_key")
        if not isinstance(payload.get("ticker"), str) or not payload["ticker"].strip():
            raise ValueError("ticker is required")
        if payload.get("direction") not in {"long", "short"}:
            raise ValueError("direction must be long or short")
        price = payload.get("entry_price")
        if not isinstance(price, Decimal) or not price.is_finite() or price == 0:
            raise ValueError("entry_price must be a finite nonzero Decimal")
        asset = payload.get("asset_type", "stock")
        sizing = [k for k in ("size_pct", "position_value_usd") if k in payload]
        if asset == "stock":
            if price <= 0 or len(sizing) != 1:
                raise ValueError("stocks require positive price and exactly one sizing field")
            size = payload[sizing[0]]
            if not isinstance(size, Decimal) or not size.is_finite() or size <= 0:
                raise ValueError("stock size must be a positive finite Decimal")
            if sizing[0] == "size_pct" and size > 25:
                raise ValueError("size_pct exceeds 25 percent")
        elif asset == "option":
            if sizing or not isinstance(payload.get("option"), dict):
                raise ValueError("options require option details and forbid caller sizing")
        else:
            raise ValueError("unsupported asset_type")
        return self._request("POST", "/trades", payload={**payload, "idempotency_key": idempotency_key})

    def update_trade(self, trade_id: str, **updates: Any) -> dict[str, Any]:
        if not updates or set(updates) - {"stop_loss", "target", "tags", "thesis_update"}:
            raise ValueError("only stop_loss, target, tags, and thesis_update can be updated")
        return self._request("PATCH", f"/trades/{_identifier(trade_id)}", payload=updates)

    def close_trade(self, trade_id: str, *, close_reason: str = "manual") -> dict[str, Any]:
        """Close once at the platform's price. No partial-close or retry assumption."""
        return self._request("POST", f"/trades/{_identifier(trade_id)}/close",
                             payload={"close_reason": close_reason})
