"""SIG Predictions venue facade, exclusively backed by the published Scalar SDK.

No urllib/httpx request construction or handwritten REST endpoint dispatcher.
SDK retries are forbidden, including for reads, to ensure one logical signed
request is never retransmitted implicitly. Trading remains opt-in and must be
routed through application risk and durable idempotency guards.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from exchanges.sig_predictions.config import SigPredictionsVenueConfig
from exchanges.sig_predictions.types import (
    OrderAction, OutcomeSide, SigBookLevel, SigBookVersion,
    SigOrderBookSnapshot, SigOrderRequest, SigOrderResult,
)


class SigApiError(RuntimeError):
    """Normalized SDK HTTP error without hiding venue error-code semantics."""

    def __init__(self, *, status: int, code: str, message: str,
                 details: Any = None, retry_after: str | None = None) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        # Advisory for human recovery, NEVER an automatic write retry policy.
        return (self.status == 429 or
                (self.status == 409 and self.code == "REQUEST_IN_FLIGHT") or
                self.status in {502, 503})


class SigPredictionsVenueClient:
    """Preserve the venue-neutral facade; delegate all network I/O to Scalar."""

    def __init__(
        self,
        config: SigPredictionsVenueConfig | None = None,
        *,
        sdk: Any = None,
        timeout_seconds: float = 10.0,
        allow_trading: bool = False,
    ) -> None:
        self.config = config or SigPredictionsVenueConfig()
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self.allow_trading = allow_trading
        if sdk is None:
            from super_market import SuperMarketAPI
            key = self.config.api_key()
            if not key:
                raise ValueError(f"missing API key; set {self.config.api_key_env}")
            sdk = SuperMarketAPI(
                bearer_auth=key,
                base_url=self.config.api_base_url(),
                timeout=timeout_seconds,
                max_retries=0,
            )
        # Reject injected retry-enabled clients too; no unsafe accidental writes.
        if getattr(sdk, "max_retries", None) != 0:
            raise ValueError("SIG SDK must have max_retries=0")
        self.sdk = sdk

    def close(self) -> None:
        if hasattr(self.sdk, "close"):
            self.sdk.close()

    def __enter__(self) -> "SigPredictionsVenueClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _require_trading(self) -> None:
        if not self.allow_trading:
            raise PermissionError("SIG trading operations disabled; explicit approval required")

    @staticmethod
    def _dump(obj: Any) -> dict[str, Any]:
        if isinstance(obj, dict):
            return obj
        if hasattr(obj, "to_dict"):
            result = obj.to_dict(mode="json", use_api_names=True)
            if isinstance(result, dict):
                return result
        raise TypeError("SIG SDK returned an unexpected response type")

    def _call(self, method: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return self._dump(method(*args, **kwargs))
        except Exception as exc:
            # Normalize only SDK HTTP status errors; leave transport uncertainty
            # visible to callers and NEVER retry a potentially accepted order.
            from super_market import APIStatusError
            if not isinstance(exc, APIStatusError):
                raise
            body = exc.body if isinstance(exc.body, dict) else {}
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            code = str(error.get("code", "HTTP_ERROR"))
            message = str(error.get("message") or str(exc))
            raise SigApiError(
                status=exc.status_code,
                code=code, message=message,
                details=error.get("details", body),
                retry_after=exc.response.headers.get("retry-after"),
            ) from exc

    @staticmethod
    def _options(**values: Any) -> dict[str, Any]:
        return {k: v for k, v in values.items() if v is not None}

    def get_account(self) -> dict[str, Any]:
        return self._call(self.sdk.account.list)

    def list_markets(self, *, limit: int = 20, cursor: str | None = None,
                     tournament_id: str | None = None, search: str | None = None,
                     sort: str = "recent", status: str = "any") -> dict[str, Any]:
        payload = self._call(
            self.sdk.markets.list,
            **self._options(limit=limit, cursor=cursor, tournament_id=tournament_id,
                            search=search, sort=sort, status=status),
        )
        if not isinstance(payload.get("data"), list):
            raise ValueError("SDK market discovery missing data collection")
        return {"markets": payload["data"], "pagination": payload.get("pagination")}

    def get_market(self, market_id: str, *, tournament_id: str | None = None) -> dict[str, Any]:
        return self._call(self.sdk.markets.retrieve, id=str(market_id),
                          **self._options(tournament_id=tournament_id))

    def get_tournament(self, slug: str) -> dict[str, Any]:
        return self._call(self.sdk.tournaments.retrieve, slug=slug)

    def get_exchange_price(self, exchange_id: str, *,
                           tournament_id: str | None = None) -> dict[str, Any]:
        return self._call(self.sdk.exchanges.list_price, id=str(exchange_id),
                          **self._options(tournament_id=tournament_id))

    def get_order_book(self, exchange_id: str, *,
                       tournament_id: str | None = None,
                       depth: int = 200) -> SigOrderBookSnapshot:
        payload = self._call(
            self.sdk.exchanges.list_orderbook, id=str(exchange_id), depth=depth,
            **self._options(tournament_id=tournament_id),
        )
        raw_version = payload.get("asOf")
        version = (SigBookVersion(sequence=int(raw_version["sequence"]), at=str(raw_version["at"]))
                   if isinstance(raw_version, dict) else None)
        if version is not None:
            iso_at = version.at.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(iso_at)
            if parsed.tzinfo is None:
                raise ValueError("SIG book version missing timezone; cannot establish freshness")
            timestamp_ms = int(parsed.timestamp() * 1000)
        else:
            # No fake local timestamp: force the live smoke to reject unversioned books.
            timestamp_ms = 0
        return SigOrderBookSnapshot(
            exchange_id=str(payload["exchangeId"]),
            market_id=str(payload["marketId"]),
            timestamp_ms=timestamp_ms,
            bids=tuple(SigBookLevel(str(x["price"]), str(x["quantity"])) for x in payload["bids"]),
            asks=tuple(SigBookLevel(str(x["price"]), str(x["quantity"])) for x in payload["asks"]),
            as_of=version,
        )

    @staticmethod
    def _order_fields(req: SigOrderRequest, *, require_key: bool = False) -> dict[str, Any]:
        if require_key and not req.idempotency_key:
            raise ValueError("SIG placement requires durable client idempotency key")
        if req.quantity <= 0 or req.quantity > 2147483647:
            raise ValueError("SIG placement quantity out of range")
        return SigPredictionsVenueClient._options(
            exchange_id=req.exchange_id, side=req.outcome.value,
            action=req.action.value, quantity=req.quantity, price=req.price,
            tournament_id=req.tournament_id, expiration_date=req.expiration_date,
        )

    def place_order(self, request: SigOrderRequest) -> SigOrderResult:
        self._require_trading()
        payload = self._call(
            self.sdk.orders.create, **self._order_fields(request, require_key=True),
            idempotency_key=request.idempotency_key,
        )
        return SigOrderResult(
            order_id=payload["orderId"],
            exchange_id=str(payload["exchangeId"]),
            open=bool(payload["open"]),
            quantity_traded=str(payload["quantityTraded"]),
            total_cost=str(payload["totalCost"]),
            fill_price=str(payload["fillPrice"]) if payload.get("fillPrice") is not None else None,
            remaining_quantity=(str(payload["remainingQuantity"])
                                if payload.get("remainingQuantity") is not None else None),
            action=OrderAction(payload["action"]) if payload.get("action") is not None else None,
            outcome=OutcomeSide(payload["side"]) if payload.get("side") is not None else None,
            price=str(payload["price"]) if payload.get("price") is not None else None,
            quantity=int(payload["quantity"]) if payload.get("quantity") is not None else None,
            terminal_reason_code=payload.get("terminalReasonCode"),
        )

    def place_multi_leg(self, requests: list[SigOrderRequest], *, idempotency_key: str,
                        relationship_constraint: str | None = None) -> dict[str, Any]:
        self._require_trading()
        if not 1 <= len(requests) <= 10 or not idempotency_key:
            raise ValueError("SIG multi-leg requires 1–10 orders and an idempotency key")
        return self._call(
            self.sdk.orders.create_multi_leg, legs=[self._order_fields(r) for r in requests],
            idempotency_key=idempotency_key,
            **self._options(relationship_constraint=relationship_constraint),
        )

    def place_batch(self, requests: list[SigOrderRequest], *,
                    idempotency_key: str) -> dict[str, Any]:
        self._require_trading()
        if not 1 <= len(requests) <= 50 or not idempotency_key:
            raise ValueError("SIG batch requires 1–50 orders and an idempotency key")
        return self._call(
            self.sdk.orders.create_batch, orders=[self._order_fields(r) for r in requests],
            idempotency_key=idempotency_key,
        )

    def list_orders(self, *, status: str = "open", exchange_id: str | None = None,
                    market_id: str | None = None, tournament_id: str | None = None,
                    limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        data = self._call(
            self.sdk.orders.list,
            **self._options(status=status, exchange_id=exchange_id, market_id=market_id,
                            tournament_id=tournament_id, limit=limit, cursor=cursor),
        )
        if not isinstance(data.get("data"), list):
            raise ValueError("SDK order list missing data; no safe preflight")
        pagination = data.get("pagination")
        if not isinstance(pagination, dict) or "hasMore" not in pagination:
            raise ValueError("SDK order list missing pagination; no safe preflight")
        # Normalize the new Scalar data/pagination schema for existing risk caller.
        return {
            "orders": data["data"],
            "nextCursor": pagination.get("nextCursor"),
            "hasMore": pagination["hasMore"],
            "coverage": data.get("coverage"),
        }

    def get_order(self, order_id: str | int) -> dict[str, Any]:
        return self._call(self.sdk.orders.retrieve, id=str(order_id))

    def get_order_fills(self, order_id: str | int, *, limit: int = 50,
                        cursor: str | None = None) -> dict[str, Any]:
        payload = self._call(self.sdk.orders.list_fills, id=str(order_id),
                             **self._options(limit=limit, cursor=cursor))
        if not isinstance(payload.get("data"), list) or not isinstance(payload.get("pagination"), dict):
            raise ValueError("SDK fills missing data/pagination; manual reconciliation required")
        return {
            "fills": payload["data"],
            "nextCursor": payload["pagination"].get("nextCursor"),
            "hasMore": payload["pagination"].get("hasMore"),
            "coverage": payload.get("coverage"),
            "totalQuantityFilled": payload.get("totalQuantityFilled"),
            "avgFillPrice": payload.get("avgFillPrice"),
        }

    def cancel_order(self, order_id: str | int) -> dict[str, Any]:
        self._require_trading()
        return self._call(self.sdk.orders.delete, id=str(order_id))

    def cancel_all_orders(self, *, exchange_id: str | None = None,
                          market_id: str | None = None,
                          tournament_id: str | None = None) -> dict[str, Any]:
        self._require_trading()
        if exchange_id is not None and market_id is not None:
            raise ValueError("exchange_id and market_id are mutually exclusive")
        if not any((exchange_id, market_id, tournament_id)):
            raise ValueError("refusing unscoped account-wide cancel-all")
        return self._call(
            self.sdk.orders.create_cancel_all,
            **self._options(exchange_id=exchange_id, market_id=market_id,
                            tournament_id=tournament_id),
        )

    def get_positions(self, *, tournament_slug: str | None = None) -> dict[str, Any]:
        if tournament_slug:
            return self._call(self.sdk.tournaments.portfolio.list_positions, slug=tournament_slug)
        return self._call(self.sdk.portfolio.list_positions)

    def get_pnl(self, *, period: str = "quarter",
                tournament_slug: str | None = None) -> dict[str, Any]:
        if tournament_slug:
            return self._call(self.sdk.tournaments.portfolio.list_pnl,
                              slug=tournament_slug, period=period)
        return self._call(self.sdk.portfolio.list_pnl, period=period)

    def mint_realtime_token(self) -> dict[str, Any]:
        # Realtime auth token, not an order; allowed in read-only diagnostics.
        return self._call(self.sdk.realtime.create_token)

    def realtime_market_channel(self, market_id: str,
                                tournament_id: str | None = None) -> str:
        tid = tournament_id or self.config.tournament_id()
        if not tid:
            raise ValueError(f"missing tournament id; set {self.config.tournament_id_env}")
        return self.config.realtime_market_channel(tid, market_id)

    @staticmethod
    def should_retry_cancel(status_code: int) -> bool:
        # Advisory only. Callers must reconcile before any cancel retry.
        return status_code == 503
