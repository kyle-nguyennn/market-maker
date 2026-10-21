"""SIG Supabase Realtime stdout probe using Phoenix wire protocol v2.

The generated Scalar SDK mints the temporary Realtime credentials. WebSocket
transport itself follows Supabase's published Phoenix protocol; SDK 0.1.0
does not expose a Realtime WebSocket listener. This is an attended observer,
not a synchronized authoritative trading feed.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

from exchanges.sig_predictions.client import SigPredictionsVenueClient


def safe_event(event: object) -> object:
    """Never dump JWTs/API keys embedded in server frames to stdout."""
    if isinstance(event, list):
        return [safe_event(v) for v in event]
    if isinstance(event, dict):
        return {
            k: ("[REDACTED]" if any(x in k.lower().replace("_", "") for x in
                   ("token", "apikey", "anonkey", "authorization", "bearerauth", "secret", "jwt"))
                else safe_event(v))
            for k, v in event.items()
        }
    return event


def emit(event: dict) -> None:
    print(json.dumps(safe_event(event), default=str, sort_keys=True), flush=True)


def socket_url(supabase_url: str, anon_key: str) -> str:
    """Construct only a Supabase websocket URL, not one derived from user input."""
    parsed = urlsplit(supabase_url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username:
        raise ValueError("unexpected Supabase project URL")
    if parsed.scheme != "https":
        raise ValueError("refusing non-TLS realtime endpoint")
    if not anon_key:
        raise ValueError("Realtime token endpoint did not provide anonKey")
    if parsed.path not in ("", "/"):
        raise ValueError("unexpected Supabase URL path")
    return urlunsplit(("wss", parsed.netloc, "/realtime/v1/websocket",
                       urlencode({"apikey": anon_key, "vsn": "2.0.0"}), ""))


def channel_name(args, token_data: dict) -> str:
    """Select exactly one documented topic; market and exchange IDs differ."""
    from apps.sig_cli import tournament_id, numeric_id
    if args.user:
        channels = token_data.get("channels")
        if not isinstance(channels, dict) or not isinstance(channels.get("user"), str):
            raise ValueError("missing user realtime channel")
        if not channels["user"].startswith("user:"):
            raise ValueError("unrecognized user channel scope")
        return channels["user"]
    if args.all_markets:
        return f"tournament:{tournament_id(args.tournament_id)}:markets"
    if getattr(args, "exchange_id", None):
        if args.tournament_id:
            raise ValueError("--exchange-id WS topic is public; use --market-id for tournament channels")
        return f"market:{numeric_id(args.exchange_id)}"
    if not getattr(args, "market_id", None):
        raise ValueError("market ID required")
    mid = numeric_id(args.market_id)
    if args.tournament_id:
        return f"tournament:{tournament_id(args.tournament_id)}:market:{mid}"
    return f"market:{mid}:combined"

def parse_phoenix_message(raw: str) -> tuple[str | None, str, str, dict, str | None]:
    """Normalize Supabase Phoenix v2 arrays; fail closed on unknown schema."""
    data = json.loads(raw)
    if not isinstance(data, list) or len(data) != 5:
        raise ValueError("unexpected realtime Phoenix frame")
    join_ref, ref, topic, event, payload = data
    if not isinstance(topic, str) or not isinstance(event, str) or not isinstance(payload, dict):
        raise ValueError("invalid realtime channel message")
    return (str(join_ref) if join_ref is not None else None,
            str(ref) if ref is not None else "",
            topic, payload, event)


def revision_alerts(raw_broadcast: dict, revisions: dict[str, int]) -> list[tuple[str, str]]:
    """SIG market/account batches carry revision and previousRevision."""
    alert: list[tuple[str, str]] = []
    kind, data = raw_broadcast.get("event"), raw_broadcast.get("payload")
    if not isinstance(data, dict):
        return [("payload", "malformed broadcast")]

    def inspect(name: str, batch: object) -> None:
        if not isinstance(batch, dict):
            alert.append((name, "malformed batch"))
            return
        delivery = batch.get("delivery")
        if not isinstance(delivery, dict):
            alert.append((name, "missing delivery metadata"))
            return
        rev, prev = delivery.get("revision"), delivery.get("previousRevision")
        if not isinstance(rev, int) or not isinstance(prev, int):
            alert.append((name, "invalid revisions"))
            return
        last = revisions.get(name)
        if last is None or rev > last:
            revisions[name] = rev
        if batch.get("resyncRequired"):
            alert.append((name, "resyncRequired"))
        elif last is not None and prev > last:
            alert.append((name, f"topic revision gap prev={prev} last={last}"))
        if any(isinstance(t, dict) and t.get("sequence") is None
               for t in batch.get("trades", [])):
            alert.append((name, "trade has null engine sequence"))

    if kind == "markets_batch":
        for market in data.get("markets", []):
            if isinstance(market, dict):
                inspect(str(market.get("marketId")), market.get("batch"))
    elif kind in {"market_batch", "account_batch"}:
        inspect("account" if kind == "account_batch" else "market", data)
    return alert


def rest_resync(client: SigPredictionsVenueClient, args, *,
                reason: str, market_id: str | None = None) -> None:
    """Bounded authoritative SDK reads after join, gap, errors and periodically.

    Aggregate discovery is not a full book snapshot. Print this explicitly,
    so the diagnostic CLI is never mistaken for a trading-ready book cache.
    """
    try:
        if args.user:
            positions = client.get_positions(tournament_slug=getattr(args, "tournament_slug", None))
            emit({"kind": "rest_resync", "reason": reason, "resource": "positions",
                  "tournamentSlug": getattr(args, "tournament_slug", None), "data": positions})
            orders = client.list_orders(status="open", tournament_id=args.tournament_id, limit=50)
            emit({"kind": "rest_resync", "reason": reason, "resource": "open_orders",
                  "data": orders["orders"], "hasMore": orders["hasMore"]})
        elif market_id or getattr(args, "market_id", None):
            mid = market_id or args.market_id
            book = client._call(client.sdk.markets.list_orderbook, id=str(mid),
                                **client._options(depth=200, tournament_id=args.tournament_id))
            emit({"kind": "rest_resync", "reason": reason, "resource": "market_orderbook",
                  "marketId": mid, "data": book})
        elif getattr(args, "exchange_id", None):
            book = client.get_order_book(args.exchange_id, depth=200)
            emit({"kind": "rest_resync", "reason": reason, "resource": "exchange_orderbook",
                  "exchangeId": args.exchange_id, "asOf": vars(book.as_of) if book.as_of else None,
                  "bids": [vars(x) for x in book.bids],
                  "asks": [vars(x) for x in book.asks]})
        else:
            markets = client.list_markets(limit=50, tournament_id=args.tournament_id, status="open")
            emit({"kind": "rest_resync", "reason": reason, "resource": "market_discovery_first_page",
                  "count": len(markets["markets"]), "pagination": markets["pagination"],
                  "warning": "not a complete multi-market book snapshot; retrieve individual books before trading"})
    except Exception as exc:
        emit({"kind": "rest_resync_failed", "reason": reason,
              "errorType": type(exc).__name__,
              "warning": "authoritative state not verified; do not trade on this realtime feed"})


async def listen_once(client: SigPredictionsVenueClient, args, *, deadline: float,
                      count: int) -> int:
    """One token and socket session; caller handles expiring credentials/reconnect."""
    from websockets.asyncio.client import connect

    credentials = client.mint_realtime_token()  # via Scalar SDK REST resource
    jwt = credentials.get("token")
    if not isinstance(jwt, str) or not jwt:
        raise ValueError("SIG realtime token response missing token")
    topic = channel_name(args, credentials)
    wire_topic = topic if topic.startswith("realtime:") else "realtime:" + topic
    url = socket_url(credentials.get("supabaseUrl", ""), credentials.get("anonKey", ""))
    # Refresh credentials before typical 3-hour token expiry.
    session_end = min(deadline, time.monotonic() + 7200)
    async with connect(url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
        join = ["1", "1", wire_topic, "phx_join", {
            "config": {
                "broadcast": {"ack": False, "self": False},
                "presence": {"key": ""},
                "postgres_changes": [],
                "private": True,
            },
            "access_token": jwt,
        }]
        await ws.send(json.dumps(join))
        emit({"kind": "connecting", "channel": topic, "at": datetime.now(UTC).isoformat()})
        # Wait for actual Supabase subscription ACK; socket connection alone
        # does not mean authenticated channel membership.
        subscribed = False
        next_heartbeat = time.monotonic() + 20
        next_resync = time.monotonic() + getattr(args, "resync_seconds", 90)
        revisions: dict[str, int] = {}
        while time.monotonic() < session_end:
            if time.monotonic() >= next_heartbeat:
                ref = str(int(time.monotonic() * 1000))
                await ws.send(json.dumps([None, ref, "phoenix", "heartbeat", {}]))
                next_heartbeat = time.monotonic() + 20
            if subscribed and time.monotonic() >= next_resync:
                rest_resync(client, args, reason="periodic")
                next_resync = time.monotonic() + getattr(args, "resync_seconds", 90)
            wait = max(0.05, min(2.0, next_heartbeat - time.monotonic(),
                                 session_end - time.monotonic()))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=wait)
            except asyncio.TimeoutError:
                continue
            join_ref, ref, received_topic, payload, event = parse_phoenix_message(raw)
            if event == "phx_reply" and received_topic == wire_topic and ref == "1":
                if payload.get("status") != "ok":
                    raise RuntimeError("SIG Realtime subscription rejected; verify token permissions")
                subscribed = True
                emit({"kind": "subscribed", "channel": topic,
                      "at": datetime.now(UTC).isoformat()})
                rest_resync(client, args, reason="join/reconnect")
                next_resync = time.monotonic() + getattr(args, "resync_seconds", 90)
                continue
            if received_topic == "phoenix" or event == "phx_reply":
                continue
            if not subscribed:
                raise RuntimeError("received events before authenticated subscription ACK")
            if event in ("phx_error", "phx_close"):
                raise RuntimeError("SIG Realtime channel closed or rejected; re-sync required")
            if event == "system" and payload.get("status") == "error":
                raise RuntimeError("SIG Realtime system reported channel error")
            if event != "broadcast":
                continue
            count += 1
            emit({"kind": "realtime_event", "receivedAt": datetime.now(UTC).isoformat(),
                  "channel": received_topic, "event": payload.get("event"),
                  "data": payload.get("payload"),
                  "sequenceStatus": "best-effort; REST resync after gaps, reconnects and periodically"})
            for name, reason in revision_alerts(payload, revisions):
                emit({"kind": "resync_required", "topic": name, "reason": reason})
                rest_resync(client, args, reason=reason,
                            market_id=name if name not in ("market", "account") else None)
            if args.max_events and count >= args.max_events:
                return count
        if not subscribed:
            raise TimeoutError("Realtime subscription not acknowledged")
        return count


async def stream(client: SigPredictionsVenueClient, args) -> None:
    """Print events until duration/count/interrupt, reconnect after lost sessions."""
    if args.all_markets:
        from apps.sig_cli import tournament_id
        tournament_id(args.tournament_id)
    deadline = time.monotonic() + args.seconds
    count = 0
    attempts = 0
    while time.monotonic() < deadline and (not args.max_events or count < args.max_events):
        try:
            count = await listen_once(client, args, deadline=deadline, count=count)
            attempts = 0
            if args.max_events and count >= args.max_events:
                break
            if time.monotonic() < deadline:
                emit({"kind": "refresh", "message": "Socket session ended; minting new token and rejoining. REST resync required."})
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:
            attempts += 1
            emit({"kind": "disconnected", "exception": type(exc).__name__,
                  "message": "Realtime is best-effort; event loss possible. REST resync required.",
                  "attempt": attempts})
            if attempts > args.reconnects:
                raise RuntimeError("realtime reconnect budget exhausted") from exc
            await asyncio.sleep(min(2 ** attempts, max(0, deadline - time.monotonic())))
    emit({"kind": "stream_complete", "eventsPrinted": count})
