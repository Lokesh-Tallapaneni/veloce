"""Cookie()-marked list parameters collect every repeated cookie value."""

from __future__ import annotations

import asyncio

import orjson

from veloce import Cookie, Veloce
from veloce.testclient import TestClient


def _run_http(app: Veloce, path: str, cookie_header: str) -> bytes:
    """Drive one HTTP request through the ASGI surface, returning the body."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"cookie", cookie_header.encode())],
        "scheme": "http",
    }
    incoming = [{"type": "http.request", "body": b"", "more_body": False}]
    sent: list[dict] = []

    async def receive() -> dict:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app(scope, receive, send))
    finally:
        loop.close()
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def test_single_cookie_value_is_one_item_list():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(pref: list[str] = Cookie(default=[])):
        return {"pref": pref}

    with TestClient(app) as client:
        client.cookies["pref"] = "dark"
        resp = client.get("/x")

    assert resp.json() == {"pref": ["dark"]}


def test_missing_optional_cookie_list_uses_default():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(pref: list[str] = Cookie(default=["light"])):
        return {"pref": pref}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.json() == {"pref": ["light"]}


def test_missing_required_cookie_list_is_422():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(pref: list[str] = Cookie()):
        return {"pref": pref}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.status_code == 422


def test_repeated_cookies_collected():
    """RFC 6265 section 5.4: duplicate names collapse to first occurrence."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(tag: list[str] = Cookie(default=[])):
        return {"tag": tag}

    body = _run_http(app, "/x", "tag=a; tag=b; tag=c")
    assert orjson.loads(body) == {"tag": ["a"]}
