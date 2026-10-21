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
                   ("token", "apikey", "authorization", "secret", "jwt"))
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
    if args.user:
        channels = token_data.get("channels")
        if not isinstance(channels, dict) or not isinstance(channels.get("user"), str):
            raise ValueError("missing user realtime channel")
        return channels["user"]
    from apps.sig_cli import tournament_id
    tid = tournament_id(args.tournament_id)
    if args.all_markets:
        return f"tournament:{tid}:markets"
    if not args.market_id or not args.market_id.isdigit() or int(args.market_id) <= 0:
        raise ValueError("market ID must be a positive numeric string")
    # Unlike order placement, a Realtime market channel is keyed by marketId,
    # not exchangeId. Multiple exchanges can belong to one market.
    return f"tournament:{tid}:market:{args.market_id}"


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
        while time.monotonic() < session_end:
            if time.monotonic() >= next_heartbeat:
                ref = str(int(time.monotonic() * 1000))
                await ws.send(json.dumps([None, ref, "phoenix", "heartbeat", {}]))
                next_heartbeat = time.monotonic() + 20
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
                continue
            if received_topic == "phoenix" or event == "phx_reply":
                continue
            if not subscribed:
                raise RuntimeError("received events before authenticated subscription ACK")
            if event in ("phx_error", "phx_close"):
                raise RuntimeError("SIG Realtime channel closed or rejected; re-sync required")
            count += 1
            emit({"kind": "realtime_event", "receivedAt": datetime.now(UTC).isoformat(),
                  "channel": received_topic, "event": event, "data": payload,
                  "sequenceStatus": "best-effort; reconcile missed updates through REST"})
            if args.max_events and count >= args.max_events:
                return count
        if not subscribed:
            raise TimeoutError("Realtime subscription not acknowledged")
        return count


async def stream(client: SigPredictionsVenueClient, args) -> None:
    """Print events until duration/count/interrupt, reconnect after lost sessions."""
    if args.user is False:
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
