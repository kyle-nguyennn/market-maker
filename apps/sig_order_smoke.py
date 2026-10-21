"""One-shot SIG Predictions order/cancel smoke. GET-only unless --live.

Unlike Hyperliquid, SIG has no post-only flag. A limit may execute despite a
non-crossing snapshot; never describe this as a guaranteed resting-order test.
No automatic POST retry: ambiguous sends require human/authoritative recovery.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from exchanges.sig_predictions.client import SigPredictionsVenueClient
from exchanges.sig_predictions.types import SigMarketSpec
from market_handler.sig_predictions_adapter import SigPredictionsMarketAdapter
from model.order import OrderIntent, Side

MIN_PRICE = Decimal("0.005")
MAX_SHARES = 1
TERMINAL = frozenset({"canceled", "cancelled", "filled", "expired", "rejected", "closed"})


class SmokeUncertain(RuntimeError):
    """Live state cannot be confirmed. Do not submit a replacement order."""


def _positive_exchange_id(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("exchange-id must be a positive numeric SIG exchange ID")
    return value


def _order_rows(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("orders"), list):
        raise SmokeUncertain("unrecognized GET /orders response; cannot prove no outstanding orders")
    orders = payload["orders"]
    if any(not isinstance(row, dict) for row in orders):
        raise SmokeUncertain("malformed open-order entry")
    if payload.get("nextCursor") or payload.get("cursor"):
        raise SmokeUncertain("open orders are paginated; manual reconciliation required")
    return orders


def _terminal_evidence(payload: object) -> bool:
    """Require positive evidence that the specific order is no longer resting."""
    if not isinstance(payload, dict):
        return False
    row = payload.get("order") if isinstance(payload.get("order"), dict) else payload
    if not isinstance(row, dict):
        return False
    status = row.get("status")
    if isinstance(status, str):
        if status.lower() in TERMINAL:
            return True
        if status.lower() in {"open", "resting", "pending"}:
            return False
    return row.get("open") is False


def _write_state(path: Path, state: dict, *, initial: bool = False) -> None:
    """No secrets; fsync before POST. Never overwrite an existing initial run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if initial:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    else:
        tmp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    # Persist the directory entry as well (on POSIX).
    if hasattr(os, "O_DIRECTORY"):
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _read_state(path: Path, exchange_id: str, tournament_id: str) -> dict:
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or (state.get("exchange_id"), state.get("tournament_id")) != (exchange_id, tournament_id):
        raise SmokeUncertain("state file has wrong exchange/tournament; do not operate on it")
    return state


def _verify_closed(client: SigPredictionsVenueClient, order_id: int | str) -> bool:
    for attempt in range(3):
        evidence = client.get_order(order_id)
        if _terminal_evidence(evidence):
            return True
        if attempt != 2:
            time.sleep(0.25)
    return False


def _report_fills(client: SigPredictionsVenueClient, order_id: int | str) -> None:
    """Read order-specific fills; never mistake terminal cancellation for no fill."""
    try:
        payload = client.get_order_fills(order_id)
    except Exception as exc:
        print(f"Fills: lookup unsuccessful ({type(exc).__name__}); manual reconciliation required.")
        return
    fills = payload.get("fills") if isinstance(payload, dict) else None
    if not isinstance(fills, list):
        print("Fills: unrecognized API response; inspect order fills and portfolio manually.")
        return
    cursor = payload.get("nextCursor") if isinstance(payload, dict) else None
    if cursor:
        print(f"Fills: at least {len(fills)} record(s), more pages; inspect all fills manually.")
    else:
        print(f"Fills: {len(fills)} execution record(s). " +
              ("A real trade occurred; reconcile portfolio positions." if fills else
               "No executions shown for this order."))
    return


def _cancel_and_verify(
    client: SigPredictionsVenueClient, path: Path, state: dict, *,
    check_first: bool = True,
) -> None:
    oid = state.get("order_id")
    if not isinstance(oid, (int, str)) or isinstance(oid, bool) or not str(oid).isdigit():
        raise SmokeUncertain("order ID unknown; no safe targeted cancel. Inspect account/orders manually")
    if check_first and _verify_closed(client, oid):
        state["phase"] = "VERIFIED_TERMINAL"
        _write_state(path, state)
        print(f"Order {oid}: venue confirms terminal.")
        _report_fills(client, oid)
        return
    # Only cancel the specific smoke order, never use account-wide cancel-all.
    client.cancel_order(oid)
    state["phase"] = "CANCEL_REQUESTED"
    _write_state(path, state)
    if not _verify_closed(client, oid):
        raise SmokeUncertain(f"cancel requested for {oid}, terminal status not confirmed; recover manually")
    state["phase"] = "VERIFIED_TERMINAL"
    _write_state(path, state)
    print(f"Order {oid}: cancellation verified non-resting.")
    _report_fills(client, oid)


def smoke(
    client: SigPredictionsVenueClient, *,
    exchange_id: str,
    tournament_id: str,
    state_path: Path,
    live: bool,
    max_book_age_s: int = 600,
    recover: bool = False,
) -> None:
    exchange_id = _positive_exchange_id(exchange_id)
    if not tournament_id or not re.fullmatch(r"[0-9a-fA-F-]{36}", tournament_id):
        raise ValueError("explicit tournament UUID required; use --tournament-id or SIG_PREDICTIONS_TOURNAMENT_ID")
    if recover:
        if not live or not client.allow_trading:
            raise PermissionError("order recovery/cancel requires explicit live authorization")
        state = _read_state(state_path, exchange_id, tournament_id)
        if state.get("phase") == "VERIFIED_TERMINAL":
            print("Previous smoke order already verified terminal; no new order sent.")
            return
        _cancel_and_verify(client, state_path, state)
        return

    # All preflights use the venue's authoritative REST data.
    client.get_account()
    book = client.get_order_book(exchange_id, tournament_id=tournament_id, depth=20)
    if book.exchange_id != exchange_id:
        raise SmokeUncertain("order book returned wrong exchange ID")
    adapter = SigPredictionsMarketAdapter()
    state = adapter.snapshot(book)
    if not state.is_valid or book.as_of is None:
        raise SmokeUncertain("book missing valid two-sided prices and version; choose another exchange")
    age_s = datetime.now(UTC).timestamp() - book.timestamp_ms / 1000
    if age_s < -2 or age_s > max_book_age_s:
        raise SmokeUncertain("venue order book version too old; no live order allowed")
    if state.best_ask is None or state.best_ask <= MIN_PRICE + Decimal("0.005"):
        raise SmokeUncertain("best ask too close to minimum limit; cannot establish non-crossing quote")
    orders = _order_rows(client.list_orders(
        status="open", exchange_id=exchange_id, tournament_id=tournament_id, limit=50,
    ))
    if orders:
        raise SmokeUncertain("existing open orders on selected exchange; isolate or reconcile first")

    key = "sig-smoke-" + uuid4().hex
    intent = OrderIntent(
        instrument=adapter.internal_instrument(exchange_id),
        side=Side.BID,
        price=MIN_PRICE,
        size=Decimal(MAX_SHARES),
        post_only=False,  # SIG's API has no post-only guarantee.
        client_order_id=key,
    )
    request = adapter.to_venue_order(
        intent, SigMarketSpec(
            market_id=book.market_id, exchange_id=exchange_id,
            tournament_id=tournament_id,
        )
    )
    print(
        f"Preflight: tournament={tournament_id} exchange={exchange_id}, "
        f"YES bid={state.best_bid}, ask={state.best_ask}, "
        f"book_version={book.as_of.sequence}. "
        f"Proposed BUY YES {MAX_SHARES} @ {MIN_PRICE} SUSQies."
    )
    print("Risk: SIG does not guarantee post-only; order may immediately fill.")
    if not live:
        print("DRY RUN complete. No POST or DELETE was sent.")
        return
    if not client.allow_trading:
        raise PermissionError("client is in read-only mode")
    expires = (datetime.now(UTC) + timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
    from dataclasses import replace
    request = replace(request, expiration_date=expires)
    record = {
        "schema": 1, "exchange_id": exchange_id, "tournament_id": tournament_id,
        "idempotency_key": key, "order_id": None, "phase": "PREPARED",
        "quantity": MAX_SHARES, "price": str(MIN_PRICE), "expiration_date": expires,
        "created_at": datetime.now(UTC).isoformat(),
    }
    _write_state(state_path, record, initial=True)  # fsync BEFORE POST
    print(f"Durable smoke state: {state_path}. Sending one order; no automatic retry.")
    try:
        result = client.place_order(request)
    except Exception as exc:
        print(
            "Order response uncertain/rejected. State remains PREPARED; "
            "check exchange orders/fills before any replacement. "
            f"Exception type: {type(exc).__name__}"
        )
        raise
    record["order_id"] = result.order_id
    record["phase"] = "ACKED" if result.order_id is not None else "NO_ORDER_ID"
    record["filled_qty_at_submit"] = result.quantity_traded
    record["open_at_submit"] = result.open
    _write_state(state_path, record)
    print(
        f"ACK: order_id={result.order_id}, open={result.open}, "
        f"filled={result.quantity_traded}, remaining={result.remaining_quantity}."
    )
    if result.exchange_id != exchange_id:
        raise SmokeUncertain("POST result exchange ID mismatch; journal retains OID for investigation")
    if result.order_id is None:
        if not result.open and Decimal(result.quantity_traded) == 0:
            record["phase"] = "VERIFIED_TERMINAL"
            _write_state(state_path, record)
            print("No resting order created; submit/cancel lifecycle not exercised.")
            return
        raise SmokeUncertain("missing order ID with possible execution; manual position/fill review required")
    # If the order filled instantly it may already be terminal; still verify.
    # If cancel/GET fails, leave the journal for --recover; do not place again.
    _cancel_and_verify(client, state_path, record, check_first=not result.open)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SIG one-order smoke: dry-run by default")
    parser.add_argument("--exchange-id", required=True, help="numeric exchange ID, not market ID")
    parser.add_argument("--tournament-id", default=os.getenv("SIG_PREDICTIONS_TOURNAMENT_ID"),
                        help="exact tournament UUID (or SIG_PREDICTIONS_TOURNAMENT_ID)")
    parser.add_argument("--state-path", type=Path,
                        help="persistent journal; default data/state/sig-smoke-<tournament>-<exchange>.json")
    parser.add_argument("--max-book-age-seconds", type=int, default=600)
    parser.add_argument("--live", action="store_true", help="enable one real order and targeted cancel")
    parser.add_argument("--confirm-exchange-id", help="must match --exchange-id to enable signed action")
    parser.add_argument("--accept-fill-risk", action="store_true", help="acknowledge no post-only guarantee")
    parser.add_argument("--recover", action="store_true", help="cancel/verify a prior journaled order; never submit")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.max_book_age_seconds <= 0:
        raise SystemExit("--max-book-age-seconds must be positive")
    exchange_id = _positive_exchange_id(args.exchange_id)
    tournament_id = args.tournament_id
    if not tournament_id:
        raise SystemExit("set --tournament-id (the competition UUID) first")
    path = args.state_path or Path(
        f"data/state/sig-smoke-{tournament_id}-{exchange_id}.json"
    )
    if args.live and (
        args.confirm_exchange_id != exchange_id or not args.accept_fill_risk
    ):
        raise SystemExit("live requires --confirm-exchange-id <same ID> and --accept-fill-risk")
    if args.recover and not args.live:
        raise SystemExit("--recover requires --live and confirmation (it may send a cancel)")
    client = SigPredictionsVenueClient(allow_trading=args.live)
    smoke(
        client, exchange_id=exchange_id, tournament_id=tournament_id,
        state_path=path, live=args.live,
        max_book_age_s=args.max_book_age_seconds, recover=args.recover,
    )


if __name__ == "__main__":
    main()
