from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.types import Cloid

from .config import HyperliquidVenueConfig
from .types import (HyperliquidL2Snapshot, OrderSide, VenueOrderRequest,
                    VenueOrderResult, parse_l2_snapshot)


_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_CLOID = re.compile(r"0x[0-9a-fA-F]{32}\Z")
_PUBLIC_CHANNELS = frozenset({"l2Book", "bbo", "trades", "activeAssetCtx"})
_PRIVATE_CHANNELS = frozenset({"userFills", "orderUpdates", "userEvents", "userFundings"})


def _team_address(address: str) -> str:
    if not _ADDRESS.fullmatch(address):
        raise ValueError("team_wallet must be a 0x-prefixed, 20-byte address")
    return address


def _sdk_float(text: str) -> float:
    """Lossless conversion at the SDK's 8-decimal wire precision boundary.

    Internal strategy and adapter quantities remain Decimal. SDK 0.24.0 expects
    floats and formats them with eight decimals; reject any lossy conversion.
    """
    try:
        decimal_value = Decimal(text)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid venue number") from exc
    if not decimal_value.is_finite() or decimal_value <= 0:
        raise ValueError("venue price and size must be finite and positive")
    number = float(decimal_value)
    if Decimal(f"{number:.8f}") != decimal_value:
        raise ValueError("venue value cannot be represented by SDK at 8 decimals")
    return number


def _operation_status(response: Any, operation: str) -> Any:
    """Validate both request-level and per-operation Hyperliquid statuses."""
    if not isinstance(response, dict):
        raise RuntimeError(f"{operation}: malformed response")
    if response.get("status") != "ok":
        raise RuntimeError(f"{operation}: request rejected: {response.get('response', response.get('status'))}")
    body = response.get("response")
    if not isinstance(body, dict):
        raise RuntimeError(f"{operation}: missing response payload")
    data = body.get("data")
    statuses = data.get("statuses") if isinstance(data, dict) else None
    if not isinstance(statuses, list) or len(statuses) != 1:
        raise RuntimeError(f"{operation}: expected one per-operation status")
    return statuses[0]


class HyperliquidVenueClient:
    """SDK-facing Hyperliquid venue client for the selected DEX profile.

    All account reads use the team wallet. The approved agent's private key is
    loaded by the caller from the environment; it is never stored in config.
    All actions require explicit connect_exchange() before they can run.
    """

    def __init__(self, config: HyperliquidVenueConfig | None = None) -> None:
        # The same validated profile governs REST, WebSocket, symbols and signing.
        self.config = config or HyperliquidVenueConfig.from_environment()
        self._info: Any | None = None
        self._ws_info: Any | None = None
        self._exchange: Any | None = None
        self._trading_account: str | None = None

    def _symbol(self, instrument: str) -> str:
        symbol = self.config.venue_symbol(instrument)
        if not instrument or symbol == f"{self.config.dex}:":
            raise ValueError("instrument is required")
        return symbol

    def connect_info(self) -> None:
        """Read-only SDK; skip spawning a WebSocket thread."""
        if self._info is None:
            self._info = Info(
                self.config.api_url, skip_ws=True, perp_dexs=[self.config.dex]
            )

    def _read(self) -> Any:
        self.connect_info()
        return self._info

    def query_meta(self) -> Any:
        return self._read().meta(dex=self.config.dex)

    def query_meta_and_asset_contexts(self) -> Any:
        # In SDK 0.24.0 meta_and_asset_ctxs() has NO dex parameter.
        # Its convenience method silently selects the native dex.
        return self._read().post(
            "/info", {"type": "metaAndAssetCtxs", "dex": self.config.dex}
        )

    def query_all_mids(self) -> Any:
        return self._read().all_mids(dex=self.config.dex)

    def query_l2_book(self, instrument: str) -> HyperliquidL2Snapshot:
        """Return a structurally validated, typed full-depth L2 snapshot."""
        book = parse_l2_snapshot(self._read().l2_snapshot(self._symbol(instrument)))
        if book["coin"] != self._symbol(instrument):
            raise ValueError("Hyperliquid L2 response belongs to a different instrument/DEX")
        return book

    def query_spot_balance(self, team_wallet: str) -> Any:
        return self._read().spot_user_state(_team_address(team_wallet))

    def query_positions(self, team_wallet: str) -> Any:
        return self._read().user_state(_team_address(team_wallet), dex=self.config.dex)

    def query_account_mode(self, team_wallet: str) -> str:
        """Read the team wallet's actual mode, not an operator assumption."""
        result = self._read().query_user_abstraction_state(_team_address(team_wallet))
        if result not in ("unifiedAccount", "standard"):
            raise ValueError("unrecognized userAbstraction account mode")
        return result

    def query_active_asset_data(self, team_wallet: str, instrument: str) -> Any:
        """Read user+coin availableToTrade/maxTradeSzs without signing.

        Response is raw SDK JSON. Do not treat availableToTrade notional as
        base-asset quantity; margin-mode normalization is a separate step.
        """
        return self._read().post(
            "/info",
            {"type": "activeAssetData", "user": _team_address(team_wallet),
             "coin": self._symbol(instrument)},
        )

    def query_open_orders(self, team_wallet: str) -> Any:
        return self._read().open_orders(_team_address(team_wallet), dex=self.config.dex)

    def query_fills(self, team_wallet: str) -> Any:
        fills = self._read().user_fills(_team_address(team_wallet))
        if not isinstance(fills, list):
            raise RuntimeError("malformed user fills response")
        return [fill for fill in fills if fill.get("coin", "").startswith(f"{self.config.dex}:")]

    def query_order_status(self, team_wallet: str, *, order_id: int | None = None,
                           client_order_id: str | None = None) -> Any:
        if (order_id is None) == (client_order_id is None):
            raise ValueError("provide exactly one of order_id or client_order_id")
        wallet = _team_address(team_wallet)
        info = self._read()
        if order_id is not None:
            if order_id < 0:
                raise ValueError("invalid order_id")
            return info.query_order_by_oid(wallet, order_id)
        return info.query_order_by_cloid(wallet, self._cloid(client_order_id))

    def query_account_state(self, team_wallet: str) -> dict[str, Any]:
        wallet = _team_address(team_wallet)
        return {
            "spot": self.query_spot_balance(wallet),  # USDPR on par, USDP on pas
            "perps": self.query_positions(wallet),
            "open_orders": self.query_open_orders(wallet),
        }

    @staticmethod
    def _cloid(raw: str | None) -> Any:
        if raw is None or not _CLOID.fullmatch(raw):
            raise ValueError("client_order_id must be 0x followed by 32 hex characters")

        return Cloid.from_str(raw)

    def connect_exchange(self, *, team_wallet: str, api_private_key: str) -> None:
        """Configure the already-approved agent signer; does NOT approve a key."""
        # Selecting pas or supplying a secret never silently enables live trading.
        if self.config.dex == "pas" and not self.config.allow_live_trading:
            raise PermissionError(
                "pas trading is disabled. Verify network, live account funding, "
                "API authorization and risk limits, then explicitly set "
                "PERPETUAL_ALPHA_PAS_ALLOW_TRADING=true."
            )
        wallet = _team_address(team_wallet)
        if not api_private_key:
            raise ValueError("API wallet private key is required")

        agent = Account.from_key(api_private_key)
        if agent.address.lower() == wallet.lower():
            raise ValueError("do not trade using the team's master wallet key")
        self._exchange = Exchange(
            agent, self.config.api_url, account_address=wallet,
            perp_dexs=[self.config.dex],
        )
        self._trading_account = wallet

    def _trader(self) -> Any:
        if self._exchange is None:
            raise RuntimeError("trading disabled: connect an approved API wallet first")
        return self._exchange

    def place_order(self, request: VenueOrderRequest) -> VenueOrderResult:
        """Submit ONE order. Inspect nested rejection even for status=ok."""
        exchange = self._trader()
        symbol = self._symbol(request.instrument)
        cloid = self._cloid(request.client_order_id) if request.client_order_id else None
        result = exchange.order(
            symbol,
            request.side is OrderSide.BUY,
            _sdk_float(request.size),
            _sdk_float(request.price),
            {"limit": {"tif": request.tif.value}},
            reduce_only=request.reduce_only,
            cloid=cloid,
        )
        entry = _operation_status(result, "order")
        if not isinstance(entry, dict):
            raise RuntimeError("order: unrecognized per-operation result")
        if "error" in entry:
            return VenueOrderResult(status="error", error=str(entry["error"]))
        if "resting" in entry:
            data = entry["resting"]
            return VenueOrderResult(status="resting", order_id=int(data["oid"]))
        if "filled" in entry:
            data = entry["filled"]
            return VenueOrderResult(
                status="filled", order_id=int(data["oid"]),
                filled_size=str(data["totalSz"]),
                average_price=str(data["avgPx"]),
            )
        raise RuntimeError("order: unrecognized status")

    def cancel_order(self, instrument: str, *, order_id: int | None = None,
                     client_order_id: str | None = None) -> bool:
        """Cancel by venue order ID or 128-bit client ID; never silently ignore errors."""
        if (order_id is None) == (client_order_id is None):
            raise ValueError("provide exactly one order identifier")
        exchange = self._trader()
        symbol = self._symbol(instrument)
        if order_id is not None:
            if order_id < 0:
                raise ValueError("invalid order_id")
            response = exchange.cancel(symbol, order_id)
        else:
            response = exchange.cancel_by_cloid(symbol, self._cloid(client_order_id))
        entry = _operation_status(response, "cancel")
        if entry == "success":
            return True
        if isinstance(entry, dict) and "error" in entry:
            raise RuntimeError(f"cancel rejected: {entry['error']}")
        raise RuntimeError(f"cancel: unrecognized status: {entry!r}")

    def schedule_cancel_all(self, deadline_ms: int) -> None:
        """Venue dead-man switch. Deadline must be refreshed while trading.

        A missed deadline requests exchange-side cancellation of all account
        orders, even if the local Python process or its host disappears.
        """
        import time
        if not isinstance(deadline_ms, int) or deadline_ms <= time.time_ns() // 1_000_000 + 5_000:
            raise ValueError("dead-man deadline must be at least five seconds ahead")
        response = self._trader().schedule_cancel(deadline_ms)
        if not isinstance(response, dict) or response.get("status") != "ok":
            raise RuntimeError(f"venue dead-man rejected: {response!r}")

    def update_leverage(self, instrument: str, leverage: int, *,
                        is_cross: bool = True) -> Any:
        if not isinstance(leverage, int) or isinstance(leverage, bool) or leverage < 1:
            raise ValueError("leverage must be a positive integer")
        # Caller is responsible for checking maxLeverage from query_meta().
        return self._trader().update_leverage(leverage, self._symbol(instrument),
                                               is_cross=is_cross)

    def connect_stream(self) -> None:
        """Create SDK-managed WebSocket independently of the REST client."""
        if self._ws_info is None:
            self._ws_info = Info(
                self.config.api_url, skip_ws=False, perp_dexs=[self.config.dex]
            )

    def subscribe_public(self, channel: str, instrument: str,
                         callback: Callable[[Any], None]) -> int:
        if channel not in _PUBLIC_CHANNELS:
            raise ValueError(f"unsupported public channel: {channel}")
        self.connect_stream()
        return self._ws_info.subscribe(
            {"type": channel, "coin": self._symbol(instrument)}, callback
        )

    def unsubscribe_public(self, channel: str, instrument: str,
                           subscription_id: int) -> bool:
        """Remove exactly one public WS subscription without closing other feeds."""
        if channel not in _PUBLIC_CHANNELS:
            raise ValueError(f"unsupported public channel: {channel}")
        if self._ws_info is None:
            return False
        return self._ws_info.unsubscribe(
            {"type": channel, "coin": self._symbol(instrument)}, subscription_id
        )

    def subscribe_private(self, channel: str, team_wallet: str,
                          callback: Callable[[Any], None]) -> int:
        if channel not in _PRIVATE_CHANNELS:
            raise ValueError(f"unsupported private channel: {channel}")
        self.connect_stream()
        return self._ws_info.subscribe(
            {"type": channel, "user": _team_address(team_wallet)}, callback
        )

    def close_stream(self) -> None:
        if self._ws_info is not None:
            self._ws_info.disconnect_websocket()
            self._ws_info = None
