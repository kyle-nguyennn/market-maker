"""Read-only Scalar SDK-backed SIG client, preserving the existing venue facade.

SDK dependency is optional to the shared framework and only installed by
requirements/sig_predictions.txt. No signed order/cancel request is exposed:
the existing attended order smoke remains on its verified transport until
its failure and partial-fill semantics have SDK parity tests.
"""
from __future__ import annotations

from typing import Any

from exchanges.sig_predictions.client import SigPredictionsVenueClient
from exchanges.sig_predictions.config import SigPredictionsVenueConfig


class SigPredictionsSDKReadClient(SigPredictionsVenueClient):
    """Use the published generated SDK for GET requests, fail closed on writes."""

    def __init__(
        self,
        config: SigPredictionsVenueConfig | None = None,
        *,
        sdk: Any = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(config, timeout_seconds=timeout_seconds, allow_trading=False)
        if sdk is None:
            from super_market import SuperMarketAPI  # optional, venue-only dependency
            api_key = self.config.api_key()
            if not api_key:
                raise ValueError(f"missing API key; set {self.config.api_key_env}")
            sdk = SuperMarketAPI(
                bearer_auth=api_key,
                base_url=self.config.api_base_url(),
                timeout=timeout_seconds,
                max_retries=0,
            )
        if getattr(sdk, "max_retries", 0) != 0:
            raise ValueError("SIG SDK must have max_retries=0; implicit trading retries are unsafe")
        self.sdk = sdk

    @staticmethod
    def _dump(response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response
        if hasattr(response, "to_dict"):
            data = response.to_dict(mode="json", use_api_names=True)
            if isinstance(data, dict):
                return data
        raise TypeError("unexpected generated SIG response; refuse implicit schema guessing")

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if method != "GET" or body is not None:
            raise PermissionError("SDK-backed SIG client is read-only pending trading parity tests")
        query = {k: v for k, v in (query or {}).items() if v is not None}
        if path == "/account":
            return self._dump(self.sdk.account.list())
        if path == "/markets":
            # Legacy venue facade expects "markets"; Scalar v0.1.0 models
            # market discovery as {"data": [...], "pagination": {...}}.
            result = self._dump(self.sdk.markets.list(
                **{
                    old: query[new]
                    for old, new in (
                        ("limit", "limit"), ("cursor", "cursor"),
                        ("tournament_id", "tournamentId"),
                        ("search", "search"), ("sort", "sort"), ("status", "status"),
                    )
                    if new in query
                }
            ))
            if not isinstance(result.get("data"), list):
                raise ValueError("SIG SDK market list missing data")
            return {"markets": result["data"], "pagination": result.get("pagination")}
        if path == "/portfolio/positions":
            return self._dump(self.sdk.portfolio.list_positions())
        if path.startswith("/tournaments/"):
            suffix = "/portfolio/positions"
            slug = path[len("/tournaments/"):]
            if slug.endswith(suffix):
                slug = slug[:-len(suffix)]
                if not slug or "/" in slug:
                    raise ValueError("invalid SIG tournament slug")
                return self._dump(self.sdk.tournaments.portfolio.list_positions(slug=slug))
            if slug and "/" not in slug:
                return self._dump(self.sdk.tournaments.retrieve(slug=slug))
        if path.startswith("/exchanges/") and path.endswith("/orderbook"):
            eid = path[len("/exchanges/"):-len("/orderbook")]
            if not eid.isdecimal() or int(eid) <= 0:
                raise ValueError("invalid SIG exchange ID")
            return self._dump(self.sdk.exchanges.list_orderbook(
                id=eid,
                **{
                    name: query[name if name == "depth" else "tournamentId"]
                    for name in ("depth", "tournament_id")
                    if (name if name == "depth" else "tournamentId") in query
                }
            ))
        raise NotImplementedError(f"SDK-backed GET endpoint not wired: {path}")

    def close(self) -> None:
        if hasattr(self.sdk, "close"):
            self.sdk.close()
