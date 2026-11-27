"""Tests verifying TestClient actually drives through the ASGI surface (T1)."""

from __future__ import annotations

from veloce import Request, Veloce
from veloce.http.response import Response
from veloce.testclient import TestClient


def test_client_dispatches_through_asgi_call_not_handle_request(monkeypatch):
    """`app.__call__` is the ASGI surface; `handle_request` is the internal
    dispatch. The new TestClient must call the former."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return {"ok": True}

    asgi_calls = {"n": 0}
    handle_calls = {"n": 0}

    # Patch at the class level — `__call__` is a special method, so Python
    # only looks it up on the type, not on the instance.
    orig_call = Veloce.__call__
    orig_handle = Veloce.handle_request

    async def counting_call(self, scope, receive, send):
        asgi_calls["n"] += 1
        await orig_call(self, scope, receive, send)

    async def counting_handle(self, req, cp=None, match=None):
        handle_calls["n"] += 1
        return await orig_handle(self, req, cp, match)

    monkeypatch.setattr(Veloce, "__call__", counting_call)
    monkeypatch.setattr(Veloce, "handle_request", counting_handle)

    client = TestClient(app)
    resp = client.get("/x")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert asgi_calls["n"] == 1, "TestClient must go through app.__call__ (ASGI)"
    # handle_request is called once by __call__ — confirms we did not double-dispatch.
    assert handle_calls["n"] == 1


def test_client_preserves_cookies_across_requests():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/set")
    async def set_cookie():

        r = Response(body=b"ok", content_type="text/plain")
        r.set_cookie("session", "abc123")
        return r

    @app.get("/check")
    async def check(request: Request):
        return {"cookies": dict(request.cookies)}

    client = TestClient(app)
    client.get("/set")
    resp = client.get("/check")
    assert resp.json()["cookies"]["session"] == "abc123"


def test_client_constructs_full_asgi_scope():
    """The scope shape constructed by TestClient must satisfy the ASGI 3.0
    HTTP spec — `type`, `method`, `path`, `query_string`, `headers`, etc."""
    app = Veloce(debug=True, openapi_url=None)
    captured: dict[str, dict] = {}

    @app.get("/probe")
    async def probe(request: Request):
        captured["scope"] = request.scope
        return {"ok": True}

    client = TestClient(app)
    client.get("/probe?x=1&y=2", headers={"X-Custom": "val"})

    scope = captured["scope"]
    assert scope, "the ASGI scope did not reach the handler"
    assert scope["type"] == "http"
    assert scope["asgi"]["version"] == "3.0"
    assert scope["method"] == "GET"
    assert scope["path"] == "/probe"
    assert scope["query_string"] == b"x=1&y=2"
    # ASGI 3.0 carries headers as lower-cased raw byte pairs, not a mapping.
    assert (b"x-custom", b"val") in scope["headers"]


def test_client_extracts_multiple_set_cookies():
    """When the app issues multiple Set-Cookie headers, TestResponse must
    parse each into the cookies dict."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/multi")
    async def multi():

        r = Response(body=b"ok", content_type="text/plain")
        r.set_cookie("a", "1")
        r.set_cookie("b", "2")
        return r

    client = TestClient(app)
    resp = client.get("/multi")
    # Both, not just the first. This asserted only `a` behind a note saying `b`
    # "may or may not survive" because `set_cookie` joins into one header -
    # which the ASGI emit path splits back out, so the hedge outlived the bug
    # that motivated it. A no-op `assert pytest is not None` stood where the
    # second assertion belonged.
    assert resp.cookies.get("a") == "1"
    assert resp.cookies.get("b") == "2"


# ── The client decodes the path like a real ASGI server ──────────────


def test_a_percent_encoded_path_segment_reaches_the_handler_decoded():
    """The client handed the raw target, so decoding bugs were invisible.

    ASGI defines `scope["path"]` as the target with percent-encoded sequences
    decoded, which is what uvicorn hands the app. The in-process client passed
    it through undecoded, so a decoding-dependent bug that appears in
    production could not be reproduced by the suite.
    """
    app = Veloce(openapi_url=None)

    @app.get("/files/{name}")
    async def download(name: str):
        return {"name": name}

    with TestClient(app) as client:
        assert client.get("/files/a%20b.txt").json() == {"name": "a b.txt"}


def test_a_plain_path_is_unchanged():
    app = Veloce(openapi_url=None)

    @app.get("/files/{name}")
    async def download(name: str):
        return {"name": name}

    with TestClient(app) as client:
        assert client.get("/files/plain.txt").json() == {"name": "plain.txt"}


def test_a_percent_encoded_non_ascii_segment_decodes_as_utf8():
    app = Veloce(openapi_url=None)

    @app.get("/files/{name}")
    async def download(name: str):
        return {"name": name}

    with TestClient(app) as client:
        assert client.get("/files/caf%C3%A9").json() == {"name": "café"}


def test_the_raw_path_stays_undecoded():
    """`raw_path` is the original target; only `path` is decoded."""
    seen = {}

    async def app(scope, receive, send):
        seen["path"] = scope["path"]
        seen["raw_path"] = scope["raw_path"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    TestClient(app).get("/files/a%20b.txt")
    assert seen["path"] == "/files/a b.txt"
    assert seen["raw_path"] == b"/files/a%20b.txt"


def test_the_query_string_is_not_touched():
    """Only the path is decoded; the query string is parsed separately."""
    app = Veloce(openapi_url=None)

    @app.get("/search")
    async def search(q: str):
        return {"q": q}

    with TestClient(app) as client:
        assert client.get("/search?q=a%20b").json() == {"q": "a b"}
