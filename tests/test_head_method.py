"""HEAD method routing + body strip (RFC 9110 §9.3.2)."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce


def _req(method: str = "GET", path: str = "/x") -> Request:
    return Request(method=method, path=path, query_string="", headers={}, body=b"")


# ── Router-level: HEAD falls back to GET match ───────────────────────


def test_router_match_head_uses_get_when_no_explicit_head():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return {}

    m = app.match("HEAD", "/x")
    assert m is not None
    assert m.route_info.handler.__name__ == "x"


def test_router_match_explicit_head_wins_over_get_fallback():
    """If the route explicitly registers HEAD, that's what HEAD matches."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x_get():
        return {"v": "get"}

    app.head("/x")(lambda: {"v": "head"})

    m = app.match("HEAD", "/x")
    assert m is not None
    # The explicit HEAD handler is the lambda, not `x_get`.
    assert m.route_info.handler.__name__ != "x_get"


# ── ASGI path: HEAD has no body, but Content-Length is real ─────────


@pytest.mark.asyncio
async def test_head_has_empty_body_via_asgi():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return {"hello": "world"}

    received: dict = {"chunks": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            received["status"] = msg["status"]
            received["headers"] = dict(msg["headers"])
        elif msg["type"] == "http.response.body":
            received["chunks"].append(msg.get("body", b""))

    scope = {
        "type": "http",
        "method": "HEAD",
        "path": "/x",
        "query_string": b"",
        "headers": [],
    }
    await app(scope, receive, send)

    assert received["status"] == 200
    assert b"".join(received["chunks"]) == b""
    # Content-Length still reports the GET-equivalent size — that's the
    # whole point of HEAD.
    assert received["headers"][b"content-length"] == b"17"  # len('{"hello":"world"}')


@pytest.mark.asyncio
async def test_get_unchanged_after_head_change():
    """Sanity: regular GET still returns the full body."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return {"hello": "world"}

    resp = await app.handle_request(_req("GET", "/x"))
    assert resp.body == b'{"hello":"world"}'
