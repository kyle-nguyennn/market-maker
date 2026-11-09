"""Offline API contracts: no registration, credentials, or network access."""
import json
from decimal import Decimal as D
from urllib.error import URLError
from urllib.request import Request

import pytest

from exchanges.alpharena import (
    AlphArenaClient, AlphArenaError, AuthenticationError, HttpResponse,
    ProtocolError, RateLimitError, TransportError,
)
from exchanges.alpharena.client import _NoRedirect, _send
from exchanges.alpharena.serialization import encode_json, decode_json


def fixture_client(status=200, body=b'{"trade_id":"abc-123","fill_price":195.50}', headers=None):
    calls = []

    def send(request, timeout):
        calls.append((request, timeout))
        return HttpResponse(status, body, headers or {})

    return AlphArenaClient("aa_test_secret", transport=send), calls


def stock(**changes):
    return {"ticker": "AAPL", "direction": "long", "entry_price": D("195.50"),
            "position_value_usd": D("1000"), **changes}


def test_exact_decimal_wire_roundtrip_including_nested_options():
    value = {"price": D("123.123456789012345678901"), "option": {"legs": [
        {"strike": D("200.00"), "qty": 1}]}, "thesis": 'quoted "text"', "flag": True}
    encoded = encode_json(value)
    assert b'123.123456789012345678901' in encoded
    assert b'"price":"' not in encoded
    assert decode_json(encoded) == value
    assert isinstance(decode_json(encoded)["price"], D)
    assert type(decode_json(encoded)["option"]["legs"][0]["qty"]) is int


@pytest.mark.parametrize("bad", [D("NaN"), D("Infinity"), 1.2, float("nan"), {1: "bad"}])
def test_nonfinite_float_and_nonstring_keys_rejected(bad):
    with pytest.raises((ValueError, TypeError)):
        encode_json({"nested": [bad]})


@pytest.mark.parametrize("body", [b'[]', b'{"x":NaN}', b'{"x":Infinity}', b'broken'])
def test_invalid_json_response(body):
    with pytest.raises(ValueError):
        decode_json(body)


def test_authentication_serialization_and_no_payload_mutation():
    client, calls = fixture_client()
    payload = stock()
    result = client.open_trade(payload, idempotency_key="intent-001")
    request, timeout = calls[0]
    assert request.full_url == "https://alpharena.io/api/v1/trades"
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer aa_test_secret"
    assert request.get_header("Content-type") == "application/json"
    assert timeout == 10
    assert decode_json(request.data)["idempotency_key"] == "intent-001"
    assert "idempotency_key" not in payload
    assert result["fill_price"] == D("195.50")
    assert "secret" not in repr(client)


@pytest.mark.parametrize("key,mode", [("", "sandbox"), ("aa_live_x", "sandbox"),
    ("aa_test_x", "leaderboard"), ("aa_test_", "sandbox"),
    ("aa_test_x\r\nInjected: x", "sandbox"), ("aa_test_x", "unknown")])
def test_key_mode_validation(key, mode):
    with pytest.raises(ValueError):
        AlphArenaClient(key, mode=mode)


def test_environment_explicit_mode(monkeypatch):
    monkeypatch.setenv("ALPHARENA_SANDBOX_API_KEY", "aa_test_x")
    monkeypatch.setenv("ALPHARENA_API_KEY", "aa_live_x")
    assert AlphArenaClient.from_environment().mode == "sandbox"
    assert AlphArenaClient.from_environment(mode="leaderboard").mode == "leaderboard"


def test_routes_and_public_reads_omit_credentials():
    client, calls = fixture_client()
    client.get_agent("my-agent")
    client.get_portfolio("my-agent")
    client.get_pnl("my-agent", period="30d")
    assert all(r.get_header("Authorization") is None for r, _ in calls)
    assert calls[-1][0].full_url.endswith("/agents/my-agent/pnl?period=30d")
    client.list_trades(status="open", page=2, limit=100)
    assert calls[-1][0].full_url.endswith("/trades?status=open&page=2&limit=100")
    client.get_trade("abc-123")
    client.update_trade("abc-123", target=D("210"), thesis_update="Updated evidence")
    assert calls[-1][0].method == "PATCH"
    client.close_trade("abc-123", close_reason="target_hit")
    assert calls[-1][0].full_url.endswith("/trades/abc-123/close")
    assert decode_json(calls[-1][0].data) == {"close_reason": "target_hit"}


@pytest.mark.parametrize("status,cls,code,unknown", [
    (401, AuthenticationError, "UNAUTHORIZED", False),
    (403, AuthenticationError, "EMAIL_NOT_VERIFIED", False),
    (429, RateLimitError, "DAILY_LIMIT_REACHED", False),
    (400, AlphArenaError, "INSUFFICIENT_CASH", False),
    (503, AlphArenaError, "UNAVAILABLE", True),
])
def test_errors_no_retry_and_no_secret_echo(status, cls, code, unknown):
    client, calls = fixture_client(status, json.dumps({"error": {
        "code": code, "message": "aa_test_secret"}}).encode(),
        {"Retry-After": "30", "X-RateLimit-Remaining": "0"})
    with pytest.raises(cls) as caught:
        client.open_trade(stock(), idempotency_key="persisted-1")
    assert len(calls) == 1
    assert caught.value.code == code
    assert caught.value.outcome_unknown is unknown
    assert caught.value.retry_after == "30"
    assert "secret" not in str(caught.value)
    assert client.last_rate_limit_headers == {"x-ratelimit-remaining": "0"}


@pytest.mark.parametrize("mutation", [False, True])
@pytest.mark.parametrize("failure", [URLError("secret"), TimeoutError("secret")])
def test_timeout_requires_reconciliation_only_for_writes(mutation, failure):
    calls = []
    def fail(request, timeout):
        calls.append(request)
        raise failure
    client = AlphArenaClient("aa_test_x", transport=fail)
    with pytest.raises(TransportError) as caught:
        client.close_trade("abc") if mutation else client.get_trade("abc")
    assert caught.value.outcome_unknown is mutation
    assert len(calls) == 1
    assert "secret" not in str(caught.value)


def test_invalid_success_is_ambiguous_and_non_json_failure_is_typed():
    client, calls = fixture_client(body=b'<html>unexpected</html>')
    with pytest.raises(ProtocolError) as caught:
        client.close_trade("abc")
    assert caught.value.outcome_unknown
    client, _ = fixture_client(status=502, body=b'<html>proxy error</html>')
    with pytest.raises(AlphArenaError) as caught:
        client.get_trade("abc")
    assert caught.value.status == 502
    assert not caught.value.outcome_unknown


def test_invalid_requests_do_not_reach_transport():
    client, calls = fixture_client()
    operations = [lambda: client.get_trade("../other"),
        lambda: client.list_trades(limit=101),
        lambda: client.update_trade("abc", thesis="replace"),
        lambda: client.open_trade(stock(), idempotency_key=""),
        lambda: client.open_trade(stock(size_pct=D("10")), idempotency_key="x"),
        lambda: client.open_trade(stock(entry_price=D("-1")), idempotency_key="x"),
        lambda: client.open_trade(stock(entry_price=1.5), idempotency_key="x"),
        lambda: client.open_trade(stock(idempotency_key="other"), idempotency_key="x")]
    for operation in operations:
        with pytest.raises((ValueError, TypeError)):
            operation()
    assert calls == []


def test_options_preserve_signed_premium_and_forbid_sizing():
    client, calls = fixture_client()
    payload = {"ticker": "SPY", "direction": "short", "asset_type": "option",
               "entry_price": D("-2.35"), "option": {"contracts": 1, "multiplier": 100,
               "legs": [{"strike": D("500"), "qty": 1}]}}
    client.open_trade(payload, idempotency_key="option-1")
    assert decode_json(calls[0][0].data)["entry_price"] == D("-2.35")
    with pytest.raises(ValueError, match="forbid caller sizing"):
        client.open_trade({**payload, "size_pct": D("5")}, idempotency_key="option-2")


def test_redirect_handler_never_forwards_authentication():
    request = Request("https://alpharena.io/api/v1/trades", headers={"Authorization": "secret"})
    assert _NoRedirect().redirect_request(request, None, 302, "", {}, "https://other.test") is None


def test_builtin_transport_reads_http_errors(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError
    import exchanges.alpharena.client as module
    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 429, "limited", {"Retry-After": "5"},
                            BytesIO(b'{"error":{"code":"RATE_LIMITED"}}'))
    monkeypatch.setattr(module, "build_opener", lambda handler: Opener())
    response = _send(Request("https://alpharena.io/api/v1/trades"), 3)
    assert response.status == 429
    assert response.headers["Retry-After"] == "5"
    assert decode_json(response.body)["error"]["code"] == "RATE_LIMITED"
