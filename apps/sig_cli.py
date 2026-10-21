"""Manual SIG API v1 testing CLI. SDK only; dry-run writes by default.

python -m apps.sig_cli --help
JSONL output; explicit tournament and exchange identifiers.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

from apps.sig_order_smoke import _write_state
from exchanges.sig_predictions.client import SigPredictionsVenueClient
from exchanges.sig_predictions.types import OrderAction, OutcomeSide, SigOrderRequest


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True), flush=True)


def dump(value):
    return SigPredictionsVenueClient._dump(value)


def numeric_id(value):
    if not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("expected positive numeric ID")
    return str(value)


def tournament_id(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value
    ):
        raise ValueError("explicit tournament UUID required (or SIG_PREDICTIONS_TOURNAMENT_ID)")
    return value


def limit_price(value):
    try:
        n = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid limit price") from exc
    if not n.is_finite() or n < Decimal("0.005") or n > Decimal("0.995"):
        raise ValueError("limit price must be 0.005–0.995; market orders not supported")
    if n % Decimal("0.005"):
        raise ValueError("limit price must use 0.005 ticks")
    return float(n)


def page_data(payload):
    if not isinstance(payload.get("data"), list) or not isinstance(payload.get("pagination"), dict):
        raise ValueError("SDK pagination schema absent; cannot continue")
    return payload["data"], payload["pagination"]


def tournaments(client, args):
    offset = 0
    for _ in range(args.pages):
        rows, page = page_data(dump(client.sdk.tournaments.list(
            status=args.status, limit=args.limit, offset=offset,
        )))
        for item in rows:
            emit({"kind": "tournament", "id": item.get("id"), "slug": item.get("slug"),
                  "name": item.get("name"), "status": item.get("status"),
                  "startDate": item.get("startDate"), "endDate": item.get("endDate"),
                  "myBalance": item.get("myBalance")})
        if not page.get("hasMore"):
            break
        if not rows:
            raise ValueError("tournament paging did not advance")
        offset += len(rows)
    emit({"kind": "hint", "message": "Use the displayed id as tournament UUID; use slug for portfolio reads."})


def markets(client, args):
    cursor = None
    for _ in range(args.pages):
        result = client.list_markets(
            limit=args.limit, cursor=cursor, search=args.search,
            status=args.status, tournament_id=args.tournament_id,
        )
        for row in result["markets"]:
            emit({"kind": "market", "marketId": row.get("id"), "title": row.get("title"),
                  "status": row.get("status"),
                  "exchanges": [
                      {"exchangeId": e.get("id"), "option": e.get("option"),
                       "latestPrice": e.get("latestPrice")}
                      for e in row.get("exchanges", [])
                  ], "contexts": row.get("contexts")})
        page = result.get("pagination") or {}
        if not page.get("hasMore"):
            break
        cursor = page.get("nextCursor")
        if not cursor:
            raise ValueError("market paging did not return cursor")


def exchanges(client, args):
    cursor = None
    for _ in range(args.pages):
        rows, page = page_data(dump(client.sdk.exchanges.list(
            limit=args.limit, **client._options(
                cursor=cursor, tournament_id=args.tournament_id,
            ),
        )))
        for item in rows:
            emit({"kind": "exchange", "exchangeId": item.get("id"),
                  "marketId": item.get("marketId"), "option": item.get("option"),
                  "latestPrice": item.get("latestPrice"), "contexts": item.get("contexts")})
        if not page.get("hasMore"):
            break
        cursor = page.get("nextCursor")
        if not cursor:
            raise ValueError("exchange paging did not return cursor")


def orders(client, args):
    cursor = None
    for _ in range(args.pages):
        result = client.list_orders(
            status=args.status, limit=args.limit, cursor=cursor,
            exchange_id=args.exchange_id, tournament_id=args.tournament_id,
        )
        for order in result["orders"]:
            emit({"kind": "order", **order})
        if not result.get("hasMore"):
            break
        cursor = result.get("nextCursor")
        if not cursor:
            raise ValueError("order paging did not return cursor")


def order_view(client, oid):
    emit({"kind": "order", **client.get_order(oid)})
    fills = client.get_order_fills(oid)
    emit({"kind": "fills", "orderId": oid, "fills": fills["fills"],
          "totalQuantityFilled": fills.get("totalQuantityFilled"),
          "avgFillPrice": fills.get("avgFillPrice"),
          "hasMore": fills.get("hasMore"), "nextCursor": fills.get("nextCursor")})


def watch_order(client, args):
    last = None
    for count in range(args.polls):
        order = client.get_order(args.order_id)
        fills = client.get_order_fills(args.order_id)
        signature = (order.get("open"), order.get("quantityFilled"),
                     fills.get("totalQuantityFilled"), fills.get("avgFillPrice"))
        if signature != last:
            emit({"kind": "order_update", "at": datetime.now(UTC).isoformat(),
                  "poll": count + 1, "order": order, "fills": fills["fills"],
                  "totalQuantityFilled": fills.get("totalQuantityFilled"),
                  "fillsHasMore": fills.get("hasMore")})
            last = signature
        if order.get("open") is False and fills.get("hasMore") is False:
            emit({"kind": "terminal_observed", "orderId": args.order_id,
                  "message": "Order no longer resting; reconcile portfolio and fills."})
            return
        if count != args.polls - 1:
            time.sleep(args.interval)
    emit({"kind": "watch_stopped", "message": "Poll limit reached; order may still be live."})


def place(client, args):
    eid = numeric_id(args.exchange_id)
    tid = tournament_id(args.tournament_id)
    price = limit_price(args.price)
    if not 1 <= args.quantity <= 2147483647:
        raise ValueError("quantity must be between 1 and 2147483647")
    if not 5 <= args.expiration_seconds <= 3600:
        raise ValueError("expiration-seconds must be 5–3600")
    # All scopes are explicit: this must be the tournament-isolated book.
    book = client.get_order_book(eid, tournament_id=tid, depth=1)
    if book.exchange_id != eid:
        raise ValueError("orderbook returned mismatching exchange ID")
    expiry = (datetime.now(UTC) + timedelta(seconds=args.expiration_seconds)).isoformat().replace("+00:00", "Z")
    key = "sig-manual-" + uuid4().hex
    request = SigOrderRequest(
        exchange_id=eid, outcome=OutcomeSide(args.side), action=OrderAction(args.action),
        quantity=args.quantity, price=price, tournament_id=tid,
        expiration_date=expiry, idempotency_key=key,
    )
    emit({"kind": "order_preview", "tournamentId": tid, "exchangeId": eid,
          "marketId": book.market_id, "side": args.side, "action": args.action,
          "quantity": args.quantity, "limitPrice": price, "expirationDate": expiry,
          "warning": "SIG has no post-only guarantee; this order can fill immediately."})
    if not args.live:
        emit({"kind": "dry_run", "message": "No order sent. Use --live with matching IDs and fill-risk acknowledgment."})
        return
    if (args.confirm_tournament_id != tid or args.confirm_exchange_id != eid
            or not args.accept_fill_risk or not client.allow_trading):
        raise PermissionError("live needs matching --confirm-tournament-id and --confirm-exchange-id plus --accept-fill-risk")
    path = args.state_dir / ("sig-manual-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex + ".json")
    journal = {
        "schema": 1, "phase": "PREPARED", "tournament_id": tid, "exchange_id": eid,
        "market_id": book.market_id, "side": args.side, "action": args.action,
        "quantity": args.quantity, "price": str(price), "expiration_date": expiry,
        "idempotency_key": key, "order_id": None, "created_at": datetime.now(UTC).isoformat(),
    }
    _write_state(path, journal, initial=True)  # fsync before signed POST
    emit({"kind": "journal_prepared", "path": str(path)})
    try:
        result = client.place_order(request)
    except Exception as exc:
        emit({"kind": "order_uncertain", "journal": str(path), "exception": type(exc).__name__,
              "message": "No retry. Inspect account orders/fills before any replacement order."})
        raise
    journal.update({
        "phase": "ACKED" if result.order_id is not None else "NO_ORDER_ID",
        "order_id": result.order_id, "open": result.open,
        "quantity_traded": result.quantity_traded,
    })
    _write_state(path, journal)
    emit({"kind": "order_ack", "orderId": result.order_id, "exchangeId": result.exchange_id,
          "open": result.open, "quantityTraded": result.quantity_traded,
          "remainingQuantity": result.remaining_quantity, "journal": str(path)})
    if result.exchange_id != eid:
        raise RuntimeError("ACK exchange mismatch; journal retained for manual recovery")


def cancel(client, args):
    oid = numeric_id(args.order_id)
    tid = tournament_id(args.tournament_id)
    order = client.get_order(oid)
    emit({"kind": "cancel_preview", "order": order})
    if order.get("tournamentId") != tid:
        raise ValueError("cancel tournament UUID mismatches venue order")
    if not args.live:
        emit({"kind": "dry_run", "message": "No cancellation sent"})
        return
    if args.confirm_order_id != oid or not client.allow_trading:
        raise PermissionError("cancellation requires --live --confirm-order-id with exact order ID")
    emit({"kind": "cancel_ack", **client.cancel_order(oid)})
    emit({"kind": "order_after_cancel", "order": client.get_order(oid)})
    emit({"kind": "note", "message": "Check fills and positions; a cancellation cannot undo executions."})


def args_parser(argv=None):
    p = argparse.ArgumentParser(prog="sig-cli", description="Manual SIG API v1 console (published Scalar SDK)")
    s = p.add_subparsers(dest="command", required=True)
    s.add_parser("account", help="authenticated account ID/balance")
    t = s.add_parser("tournaments", help="discover accessible tournament UUIDs, slugs, statuses")
    t.add_argument("--status", choices=("active", "draft", "ended", "any"), default="active")
    t.add_argument("--limit", type=int, default=50)
    t.add_argument("--pages", type=int, default=2)
    for name in ("markets", "exchanges"):
        a = s.add_parser(name, help="discover market/exchange IDs with tournament context")
        a.add_argument("--tournament-id", default=os.getenv("SIG_PREDICTIONS_TOURNAMENT_ID"))
        a.add_argument("--limit", type=int, default=50)
        a.add_argument("--pages", type=int, default=2)
        if name == "markets":
            a.add_argument("--status", choices=("open", "closed", "settled", "any"), default="open")
            a.add_argument("--search")
    td = s.add_parser("tournament", help="inspect exact tournament slug and its scoped balance")
    td.add_argument("slug")
        m = s.add_parser("market", help="market info and exchange IDs")
    m.add_argument("market_id")
    m.add_argument("--tournament-id", default=os.getenv("SIG_PREDICTIONS_TOURNAMENT_ID"))
    pp = s.add_parser("price", help="exchange price snapshot in selected tournament context")
    pp.add_argument("exchange_id", type=numeric_id)
    pp.add_argument("--tournament-id", default=os.getenv("SIG_PREDICTIONS_TOURNAMENT_ID"))
    b = s.add_parser("book", help="exchange order book")
    b.add_argument("exchange_id", type=numeric_id)
    b.add_argument("--tournament-id", default=os.getenv("SIG_PREDICTIONS_TOURNAMENT_ID"))
    b.add_argument("--depth", type=int, default=20)
    a = s.add_parser("positions", help="global or tournament-specific portfolio")
    a.add_argument("--tournament-slug")
    o = s.add_parser("orders", help="list open/closed order IDs")
    o.add_argument("--tournament-id", default=os.getenv("SIG_PREDICTIONS_TOURNAMENT_ID"))
    o.add_argument("--exchange-id")
    o.add_argument("--status", choices=("open", "closed", "expired", "all"), default="open")
    o.add_argument("--limit", type=int, default=50)
    o.add_argument("--pages", type=int, default=2)
    x = s.add_parser("order", help="inspect order and executed fills")
    x.add_argument("order_id", type=numeric_id)
    w = s.add_parser("watch-order", help="poll execution until terminal/limit")
    w.add_argument("order_id", type=numeric_id)
    w.add_argument("--interval", type=float, default=3)
    w.add_argument("--polls", type=int, default=20)
    a = s.add_parser("place", help="limit-order dry run by default; explicit live flag required")
    a.add_argument("--exchange-id", required=True)
    a.add_argument("--tournament-id", default=os.getenv("SIG_PREDICTIONS_TOURNAMENT_ID"))
    a.add_argument("--side", choices=("yes", "no"), required=True)
    a.add_argument("--action", choices=("buy", "sell"), required=True)
    a.add_argument("--quantity", required=True, type=int)
    a.add_argument("--price", required=True)
    a.add_argument("--expiration-seconds", type=int, default=120)
    a.add_argument("--state-dir", type=Path, default=Path("data/state"))
    a.add_argument("--live", action="store_true")
    a.add_argument("--confirm-exchange-id")
    a.add_argument("--confirm-tournament-id")
    a.add_argument("--accept-fill-risk", action="store_true")
    a = s.add_parser("cancel", help="targeted cancellation, dry run by default")
    a.add_argument("order_id", type=numeric_id)
    a.add_argument("--tournament-id", default=os.getenv("SIG_PREDICTIONS_TOURNAMENT_ID"))
    a.add_argument("--live", action="store_true")
    a.add_argument("--confirm-order-id")
    a = s.add_parser("stream", help="stream Supabase Realtime feeds to stdout")
    a.add_argument("--tournament-id", default=os.getenv("SIG_PREDICTIONS_TOURNAMENT_ID"))
    mode = a.add_mutually_exclusive_group(required=True)
    mode.add_argument("--market-id", help="market container ID (tournament or public combined feed)")
    mode.add_argument("--exchange-id", help="public context only: individual exchange topic")
    mode.add_argument("--all-markets", action="store_true", help="one aggregate topic for all tournament markets")
    mode.add_argument("--user", action="store_true", help="private account_batch and notification feed")
    a.add_argument("--max-events", type=int, default=0, help="0=unlimited")
    a.add_argument("--seconds", type=int, default=1800)
    a.add_argument("--tournament-slug", help="scope portfolio resync on --user events")
    a.add_argument("--resync-seconds", type=int, default=90, help="REST reconciliation cadence; min 30")
    a.add_argument("--reconnects", type=int, default=2)
    return p.parse_args(argv)


def main(argv=None):
    args = args_parser(argv)
    if hasattr(args, "limit") and not 1 <= args.limit <= 100:
        raise ValueError("--limit must be 1–100")
    if hasattr(args, "pages") and not 1 <= args.pages <= 20:
        raise ValueError("--pages must be 1–20")
    if args.command == "watch-order" and (args.interval < 2 or args.polls < 1):
        raise ValueError("watch requires --interval >= 2 and --polls >= 1")
    if args.command == "stream" and (args.seconds < 1 or args.reconnects < 0 or args.max_events < 0 or args.resync_seconds < 30):
        raise ValueError("invalid realtime duration/count")
    with SigPredictionsVenueClient(allow_trading=(
        args.command in ("place", "cancel") and args.live
    )) as client:
        if args.command == "account":
            data = client.get_account()
            emit({"kind": "account", "id": data.get("id"), "username": data.get("username"),
                  "balance": data.get("balance")})
        elif args.command == "tournaments":
            tournaments(client, args)
        elif args.command == "markets":
            markets(client, args)
        elif args.command == "exchanges":
            exchanges(client, args)
        elif args.command == "tournament":
            emit({"kind": "tournament_detail", **client.get_tournament(args.slug)})
        elif args.command == "market":
            emit({"kind": "market_detail", **client.get_market(
                args.market_id, tournament_id=args.tournament_id)})
        elif args.command == "price":
            emit({"kind": "price", **client.get_exchange_price(
                args.exchange_id, tournament_id=args.tournament_id)})
        elif args.command == "book":
            b = client.get_order_book(args.exchange_id, tournament_id=args.tournament_id,
                                      depth=args.depth)
            emit({"kind": "book", "marketId": b.market_id, "exchangeId": b.exchange_id,
                  "asOf": vars(b.as_of) if b.as_of else None,
                  "bids": [vars(x) for x in b.bids], "asks": [vars(x) for x in b.asks]})
        elif args.command == "positions":
            emit({"kind": "positions", **client.get_positions(tournament_slug=args.tournament_slug)})
        elif args.command == "orders":
            orders(client, args)
        elif args.command == "order":
            order_view(client, args.order_id)
        elif args.command == "watch-order":
            watch_order(client, args)
        elif args.command == "place":
            place(client, args)
        elif args.command == "cancel":
            cancel(client, args)
        elif args.command == "stream":
            from apps.sig_realtime_stream import stream
            import asyncio
            asyncio.run(stream(client, args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        emit({"kind": "stopped", "message": "Interrupted"})
    except (ValueError, PermissionError) as exc:
        emit({"kind": "error", "message": str(exc)})
        sys.exit(2)
