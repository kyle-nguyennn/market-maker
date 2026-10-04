from __future__ import annotations

from typing import Any

from exchanges.sig_predictions.config import SigPredictionsVenueConfig
from exchanges.sig_predictions.types import SigOrderRequest


class SigPredictionsVenueClient:
    """Thin boundary around the official SIG Predictions Cup v1 API.

    The public Product Guide and Changelog document the API permissions,
    realtime channel naming, and cancellation retry semantics. The canonical
    API Reference is:
    https://sig.thesuper.market/api/v1/docs#description/introduction

    That reference is authoritative for exact HTTP paths, auth headers, and
    request/response field names. Those wire details must not be inferred from
    WebUI traffic.
    """

    def __init__(self, config: SigPredictionsVenueConfig | None = None) -> None:
        self.config = config or SigPredictionsVenueConfig()

    @staticmethod
    def should_retry_cancel(status_code: int) -> bool:
        """Cancellation is explicitly safe to retry after HTTP 503."""
        return status_code == 503

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
    def _wire_contract_required(operation: str) -> NotImplementedError:
        return NotImplementedError(
            f"{operation} requires the exact route/auth/payload from the official "
            "SIG v1 API Reference; do not infer it from WebUI traffic"
        )

    def list_markets(self, **_: Any) -> Any:
        raise self._wire_contract_required("list_markets")

    def get_order_book(self, market_id: str, **_: Any) -> Any:
        _ = market_id
        raise self._wire_contract_required("get_order_book")

    def get_positions(self, **_: Any) -> Any:
        raise self._wire_contract_required("get_positions")

    def get_open_orders(self, **_: Any) -> Any:
        raise self._wire_contract_required("get_open_orders")

    def place_order(self, request: SigOrderRequest, **_: Any) -> Any:
        _ = request
        raise self._wire_contract_required("place_order")

    def cancel_order(self, order_id: str | int, **_: Any) -> Any:
        _ = order_id
        raise self._wire_contract_required("cancel_order")

    def cancel_all_orders(self, **_: Any) -> Any:
        raise self._wire_contract_required("cancel_all_orders")
