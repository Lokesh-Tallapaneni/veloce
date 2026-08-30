"""ASGI response correctness: multi-Set-Cookie + 204/304/205 strip body (Q44, Q47)."""

from __future__ import annotations

import pytest

from tests._asgi_drive import http_scope
from veloce import Response, Veloce
from veloce.testclient import TestClient

# ── Q44: Multi-Set-Cookie ASGI emission ────────────────────────────────


def test_multiple_set_cookies_emit_as_separate_asgi_headers():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/multi")
    async def multi():
        r = Response(body=b"ok", content_type="text/plain")
        r.set_cookie("a", "1")
        r.set_cookie("b", "2")
        r.set_cookie("c", "3")
        return r

    client = TestClient(app)
    resp = client.get("/multi")

    # Each Set-Cookie should be its own entry in the raw header list.
    set_cookie_lines = [v.decode() for k, v in resp.raw_headers if k == b"set-cookie"]
    assert len(set_cookie_lines) == 3
    assert any("a=1" in line for line in set_cookie_lines)
    assert any("b=2" in line for line in set_cookie_lines)
    assert any("c=3" in line for line in set_cookie_lines)
    # And the cookies dict picks up all three.
    assert resp.cookies == {"a": "1", "b": "2", "c": "3"}


def test_single_set_cookie_still_emits_one_header():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/one")
    async def one():
        r = Response(body=b"ok", content_type="text/plain")
        r.set_cookie("session", "abc")
        return r

    client = TestClient(app)
    resp = client.get("/one")
    cookies = [v for k, v in resp.raw_headers if k == b"set-cookie"]
    assert len(cookies) == 1
    assert resp.cookies == {"session": "abc"}


# ── Q47: 204 / 304 / 205 strip body ───────────────────────────────────


@pytest.mark.parametrize("code", [204, 205])
def test_no_body_status_codes_strip_body(code):
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x", status_code=code)
    async def h():
        return {"this": "should be stripped"}

    client = TestClient(app)
    resp = client.get("/x")
    assert resp.status_code == code
    assert resp.body == b""
    # 204/205 have no representation, so Content-Length is 0 (an intermediary
    # treats content-length: N with an empty body as malformed).
    cl = next((v.decode() for k, v in resp.raw_headers if k == b"content-length"), None)
    assert cl == "0"


def test_304_strips_body_but_advertises_representation_length():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x", status_code=304)
    async def h():
        return {"this": "cached"}

    resp = TestClient(app).get("/x")
    assert resp.status_code == 304
    assert resp.body == b""
    # A 304 (like HEAD) may advertise the would-be-200 Content-Length while
    # sending no body (RFC 9110 Sec. 8.6 / 15.4.5).
    cl = next((v.decode() for k, v in resp.raw_headers if k == b"content-length"), None)
    assert cl == str(len(b'{"this":"cached"}'))


def test_200_response_body_unchanged():
    """Sanity: only the no-body codes get stripped; 200 keeps its payload."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def h():
        return {"k": "v"}

    client = TestClient(app)
    resp = client.get("/x")
    assert resp.status_code == 200
    assert resp.body == b'{"k":"v"}'


def test_explicit_response_with_204_status_strips_body():
    """A handler that returns a Response(204, body=...) should also have its
    body stripped — the spec applies regardless of how the status was set."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def h():
        return Response(status_code=204, body=b"nope", content_type="text/plain")

    client = TestClient(app)
    resp = client.get("/x")
    assert resp.status_code == 204
    assert resp.body == b""


# ── Non-ASCII query_string → 400, not 500 ──────────────────────────────


async def test_non_ascii_query_string_returns_400():
    """Raw non-ASCII bytes in `query_string` are a client error: the ASGI path
    must emit 400, not raise UnicodeDecodeError out of dispatch as a 500."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def h():
        return {"ok": True}

    # The TestClient encodes the query string as ASCII, so drive the ASGI app
    # directly with a scope carrying raw UTF-8 bytes (`q=café` un-%-encoded).
    scope = http_scope(
        type="http", method="GET", path="/x", query_string="q=café".encode(), headers=[]
    )
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 400
