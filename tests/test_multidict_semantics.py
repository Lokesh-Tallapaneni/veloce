"""MultiDict semantics on Request.headers and Request.query_params (Q7, Q12)."""

from __future__ import annotations

from veloce import Request, Veloce
from veloce.http.datastructures import Headers, QueryParams
from veloce.testclient import TestClient

# ── QueryParams ────────────────────────────────────────────────────────


def test_query_params_repeated_keys_preserved():
    p = QueryParams.from_query_string("tag=a&tag=b&tag=c")
    assert p["tag"] == "a"  # first wins for single-value access
    assert p.getlist("tag") == ["a", "b", "c"]
    assert p.getall("tag") == ["a", "b", "c"]


def test_query_params_blank_values():
    p = QueryParams.from_query_string("a=&b=2")
    assert p["a"] == ""
    assert p["b"] == "2"


def test_query_params_percent_decoded():
    p = QueryParams.from_query_string("q=hello%20world")
    assert p["q"] == "hello world"


def test_query_params_empty_string():
    p = QueryParams.from_query_string("")
    assert len(p) == 0
    assert p.getlist("x") == []


def test_query_params_getlist_missing_returns_empty():
    p = QueryParams.from_query_string("a=1")
    assert p.getlist("missing") == []


def test_request_query_params_repeated():
    req = Request(
        method="GET",
        path="/",
        query_string="tag=a&tag=b",
        headers={},
        body=b"",
    )
    assert req.query_params.getlist("tag") == ["a", "b"]
    assert req.query_params["tag"] == "a"


# ── Headers ────────────────────────────────────────────────────────────


def test_headers_case_insensitive():
    h = Headers({"Content-Type": "application/json"})
    assert h["content-type"] == "application/json"
    assert h["CONTENT-TYPE"] == "application/json"
    assert h.get("Content-type") == "application/json"


def test_headers_preserve_duplicates_from_tuple_list():
    h = Headers(
        [
            ("Set-Cookie", "a=1"),
            ("Set-Cookie", "b=2"),
            ("X-Custom", "v"),
        ]
    )
    assert h.getlist("Set-Cookie") == ["a=1", "b=2"]
    assert h.getall("set-cookie") == ["a=1", "b=2"]
    assert h["x-custom"] == "v"


def test_headers_getlist_missing_returns_empty():
    h = Headers({"X": "1"})
    assert h.getlist("missing") == []


def test_request_headers_wrap_plain_dict():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={"Content-Type": "text/html"},
        body=b"",
    )
    assert isinstance(req.headers, Headers)
    assert req.headers["content-type"] == "text/html"


def test_request_headers_pass_through_existing_headers_instance():
    h = Headers({"X-A": "1"})
    req = Request(method="GET", path="/", query_string="", headers=h, body=b"")
    # Same instance — no re-wrapping cost.
    assert req.headers is h


# ── End-to-end via app ────────────────────────────────────────────────


def test_app_query_list_param_uses_getall():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/items")
    async def items(tag: list[str] = []):  # noqa: B006
        return {"tags": tag}

    client = TestClient(app)
    resp = client.get("/items?tag=a&tag=b&tag=c")
    assert resp.status_code == 200
    assert resp.json() == {"tags": ["a", "b", "c"]}


def test_app_headers_case_insensitive_in_handler():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/h")
    async def h(request: Request):
        # Handler reads any casing — Headers is case-insensitive.
        return {
            "lower": request.headers.get("x-custom"),
            "upper": request.headers.get("X-CUSTOM"),
            "title": request.headers.get("X-Custom"),
        }

    client = TestClient(app)
    resp = client.get("/h", headers={"X-Custom": "v"})
    j = resp.json()
    assert j["lower"] == "v"
    assert j["upper"] == "v"
    assert j["title"] == "v"


async def test_app_duplicate_request_headers_preserved():
    """When the ASGI scope carries duplicate headers, Request.headers must
    preserve them — `Forwarded` is a common duplicate-carrying header."""
    # Build a synthetic ASGI scope with duplicated headers and dispatch
    # via Veloce.__call__ — the path TestClient also takes.
    app = Veloce(debug=True, openapi_url=None)

    seen: dict = {}

    @app.get("/d")
    async def d(request: Request):
        seen["all"] = request.headers.getlist("X-Trace")
        return {"ok": True}

    # Build the scope by hand to inject duplicates that a dict couldn't carry.
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/d",
        "raw_path": b"/d",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"x-trace", b"hop1"),
            (b"x-trace", b"hop2"),
            (b"x-trace", b"hop3"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    import asyncio

    async def drive():
        body_sent = False

        async def receive():
            nonlocal body_sent
            if body_sent:
                await asyncio.Event().wait()
            body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            pass

        await app(scope, receive, send)

    await drive()
    assert seen["all"] == ["hop1", "hop2", "hop3"]


# ── pytest plugin sanity ───────────────────────────────────────────────
