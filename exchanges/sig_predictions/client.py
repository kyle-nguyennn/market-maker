from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
import time
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from exchanges.sig_predictions.config import SigPredictionsVenueConfig
from exchanges.sig_predictions.types import (
    OrderAction,
    OutcomeSide,
    SigBookLevel,
    SigBookVersion,
    SigOrderBookSnapshot,
    SigOrderRequest,
    SigOrderResult,
)


class SigApiError(RuntimeError):
    """Stable Super Market API error envelope."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        details: Any = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return (
            self.status == 429
            or (self.status == 409 and self.code == "REQUEST_IN_FLIGHT")
            or self.status in {502, 503}
        )


class SigPredictionsVenueClient:
    """Thin client for the official Super Market /api/v1 interface.

    Uses Authorization: Bearer <key>. The API also accepts X-API-Key, but the
    client intentionally emits one canonical authentication form.
    """

    def __init__(
        self,
        config: SigPredictionsVenueConfig | None = None,
        *,
        opener: Callable[..., Any] = urlopen,
        timeout_seconds: float = 10.0,
        allow_trading: bool = False,
    ) -> None:
        self.config = config or SigPredictionsVenueConfig()
        self._opener = opener
        if timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be positive')
        self.timeout_seconds = timeout_seconds
        # Explicit opt-in at construction. Read-only checks never place orders.
        self.allow_trading = allow_trading

    def _require_trading(self) -> None:
        if not self.allow_trading:
            raise PermissionError('SIG trading operations disabled; integrate risk gate before opting in')

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        api_key = self.config.api_key()
        if not api_key:
            raise ValueError(f"missing API key; set {self.config.api_key_env}")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _decode_json(raw: bytes) -> Any:
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"), parse_float=Decimal)

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.config.api_base_url()}/{path.lstrip('/')}"
        if query:
            encoded = urlencode(
                {key: value for key, value in query.items() if value is not None}
            )
            if encoded:
                url = f"{url}?{encoded}"

        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")

        request = Request(
            url,
            data=data,
            headers=self._headers(json_body=body is not None),
            method=method,
        )

        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                return self._decode_json(response.read())
        except HTTPError as exc:
            payload = self._decode_json(exc.read()) or {}
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                code = str(error.get("code", "HTTP_ERROR"))
                message = str(error.get("message", exc.reason))
                details = error.get("details")
            else:
                code = "HTTP_ERROR"
                message = str(exc.reason)
                details = payload
            raise SigApiError(
                status=exc.code,
                code=code,
                message=message,
                details=details,
                retry_after=exc.headers.get("Retry-After"),
            ) from exc

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", "/account")

    def list_markets(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        tournament_id: str | None = None,
        search: str | None = None,
        sort: str = "recent",
        status: str = "any",
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/markets",
            query={
                "limit": limit,
                "cursor": cursor,
                "tournamentId": tournament_id,
                "search": search,
                "sort": sort,
                "status": status,
            },
        )

    def get_market(
        self,
        market_id: str,
        *,
        tournament_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/markets/{market_id}",
            query={"tournamentId": tournament_id},
        )

    def get_tournament(self, slug: str) -> dict[str, Any]:
        return self._request("GET", f"/tournaments/{slug}")

    def get_exchange_price(
        self,
        exchange_id: str,
        *,
        tournament_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/exchanges/{exchange_id}/price",
            query={"tournamentId": tournament_id},
        )

    def get_order_book(
        self,
        exchange_id: str,
        *,
        tournament_id: str | None = None,
        depth: int = 200,
    ) -> SigOrderBookSnapshot:
        payload = self._request(
            "GET",
            f"/exchanges/{exchange_id}/orderbook",
            query={"depth": depth, "tournamentId": tournament_id},
        )
        as_of_raw = payload.get("asOf")
        as_of = (
            SigBookVersion(
                sequence=int(as_of_raw["sequence"]),
                at=str(as_of_raw["at"]),
            )
            if as_of_raw is not None
            else None
        )
        if as_of is not None:
            at = as_of.at[:-1] + "+00:00" if as_of.at.endswith("Z") else as_of.at
            timestamp_ms = int(datetime.fromisoformat(at).timestamp() * 1000)
        else:
            timestamp_ms = int(time.time() * 1000)

        return SigOrderBookSnapshot(
            exchange_id=str(payload["exchangeId"]),
            market_id=str(payload["marketId"]),
            timestamp_ms=timestamp_ms,
            bids=tuple(
                SigBookLevel(str(level["price"]), str(level["quantity"]))
                for level in payload["bids"]
            ),
            asks=tuple(
                SigBookLevel(str(level["price"]), str(level["quantity"]))
                for level in payload["asks"]
            ),
            as_of=as_of,
        )

    @staticmethod
    def _order_payload(request: SigOrderRequest, *, include_key: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "exchangeId": request.exchange_id,
            "side": request.outcome.value,
            "action": request.action.value,
            "quantity": request.quantity,
        }
        if request.price is not None:
            payload["price"] = request.price
        if request.tournament_id is not None:
            payload["tournamentId"] = request.tournament_id
        if request.expiration_date is not None:
            payload["expirationDate"] = request.expiration_date
        if include_key:
            payload["idempotencyKey"] = request.idempotency_key
        return payload

    def place_order(self, request: SigOrderRequest) -> SigOrderResult:
        self._require_trading()
        if not request.idempotency_key:
            raise ValueError("POST /orders requires a client-supplied idempotency key")
        payload = self._request(
            "POST",
            "/orders",
            body=self._order_payload(request, include_key=True),
        )
        return SigOrderResult(
            order_id=payload["orderId"],
            exchange_id=str(payload["exchangeId"]),
            open=bool(payload["open"]),
            quantity_traded=str(payload["quantityTraded"]),
            total_cost=str(payload["totalCost"]),
            fill_price=(
                str(payload["fillPrice"]) if payload.get("fillPrice") is not None else None
            ),
            remaining_quantity=(
                str(payload["remainingQuantity"])
                if payload.get("remainingQuantity") is not None
                else None
            ),
            action=(
                OrderAction(payload["action"])
                if payload.get("action") is not None
                else None
            ),
            outcome=(
                OutcomeSide(payload["side"])
                if payload.get("side") is not None
                else None
            ),
            price=str(payload["price"]) if payload.get("price") is not None else None,
            quantity=(
                int(payload["quantity"]) if payload.get("quantity") is not None else None
            ),
            terminal_reason_code=payload.get("terminalReasonCode"),
        )

    def place_multi_leg(
        self,
        requests: list[SigOrderRequest],
        *,
        idempotency_key: str,
        relationship_constraint: str | None = None,
    ) -> dict[str, Any]:
        self._require_trading()
        if not 1 <= len(requests) <= 10:
            raise ValueError("POST /orders/multi-leg accepts 1 to 10 legs")
        body: dict[str, Any] = {
            "idempotencyKey": idempotency_key,
            "legs": [
                self._order_payload(request, include_key=False)
                for request in requests
            ],
        }
        if relationship_constraint is not None:
            body["relationshipConstraint"] = relationship_constraint
        return self._request("POST", "/orders/multi-leg", body=body)

    def place_batch(
        self,
        requests: list[SigOrderRequest],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_trading()
        if not 1 <= len(requests) <= 50:
            raise ValueError("POST /orders/batch accepts 1 to 50 orders")
        return self._request(
            "POST",
            "/orders/batch",
            body={
                "idempotencyKey": idempotency_key,
                "orders": [
                    self._order_payload(request, include_key=False)
                    for request in requests
                ],
            },
        )

    def list_orders(
        self,
        *,
        status: str = "open",
        exchange_id: str | None = None,
        market_id: str | None = None,
        tournament_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/orders",
            query={
                "status": status,
                "exchangeId": exchange_id,
                "marketId": market_id,
                "tournamentId": tournament_id,
                "limit": limit,
                "cursor": cursor,
            },
        )

    def get_order(self, order_id: str | int) -> dict[str, Any]:
        return self._request("GET", f"/orders/{order_id}")

    def get_order_fills(
        self,
        order_id: str | int,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/orders/{order_id}/fills",
            query={"limit": limit, "cursor": cursor},
        )

    def cancel_order(self, order_id: str | int) -> dict[str, Any]:
        self._require_trading()
        return self._request("DELETE", f"/orders/{order_id}")

    def cancel_all_orders(
        self,
        *,
        exchange_id: str | None = None,
        market_id: str | None = None,
        tournament_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_trading()
        if exchange_id is not None and market_id is not None:
            raise ValueError("exchange_id and market_id are mutually exclusive")
        return self._request(
            "POST",
            "/orders/cancel-all",
            body={
                key: value
                for key, value in {
                    "exchangeId": exchange_id,
                    "marketId": market_id,
                    "tournamentId": tournament_id,
                }.items()
                if value is not None
            },
        )

    def get_positions(self, *, tournament_slug: str | None = None) -> dict[str, Any]:
        path = (
            f"/tournaments/{tournament_slug}/portfolio/positions"
            if tournament_slug
            else "/portfolio/positions"
        )
        return self._request("GET", path)

    def get_pnl(
        self,
        *,
        period: str = "quarter",
        tournament_slug: str | None = None,
    ) -> dict[str, Any]:
        if tournament_slug:
            return self._request(
                "GET",
                f"/tournaments/{tournament_slug}/portfolio/pnl",
                query={"period": period},
            )
        return self._request("GET", "/portfolio/pnl", query={"period": period})

    def mint_realtime_token(self) -> dict[str, Any]:
        return self._request("POST", "/realtime/token", body={})

    def realtime_market_channel(
        self,
        market_id: str,
        tournament_id: str | None = None,
    ) -> str:
        tournament = tournament_id or self.config.tournament_id()
        if not tournament:
            raise ValueError(
                f"missing tournament id; set {self.config.tournament_id_env}"
            )
        return self.config.realtime_market_channel(tournament, market_id)

    @staticmethod
    def should_retry_cancel(status_code: int) -> bool:
        return status_code == 503
