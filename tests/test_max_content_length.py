"""MAX_CONTENT_LENGTH → 413 enforcement."""

from __future__ import annotations

import asyncio

from veloce import Request, Veloce
from veloce.testclient import TestClient


def _app(max_size: int | None) -> Veloce:
    app = Veloce(debug=True, openapi_url=None)
    if max_size is not None:
        app.config["MAX_CONTENT_LENGTH"] = max_size

    @app.post("/echo")
    async def echo(request: Request):
        return {"received": len(await request.body())}

    return app


def test_under_limit_passes():
    client = TestClient(_app(max_size=1024))
    resp = client.post("/echo", content=b"x" * 512)
    assert resp.status_code == 200
    assert resp.json() == {"received": 512}


def test_exactly_at_limit_passes():
    """At the boundary the request is still accepted (the cap is exclusive)."""
    client = TestClient(_app(max_size=100))
    resp = client.post("/echo", content=b"x" * 100)
    assert resp.status_code == 200
    assert resp.json() == {"received": 100}


def test_over_limit_returns_413():
    client = TestClient(_app(max_size=100))
    resp = client.post("/echo", content=b"x" * 101)
    assert resp.status_code == 413
    body = resp.json()
    assert body["status_code"] == 413
    assert body["limit"] == 100


def test_explicit_none_max_content_length_allows_any_size():
    """Setting MAX_CONTENT_LENGTH=None explicitly restores unlimited bodies."""
    app = Veloce(debug=True, openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = None

    @app.post("/echo")
    async def echo(request: Request):
        return {"received": len(await request.body())}

    client = TestClient(app)
    big = b"x" * 1_000_000
    resp = client.post("/echo", content=big)
    assert resp.status_code == 200
    assert resp.json() == {"received": 1_000_000}


async def test_default_limit_rejects_oversized_declared_length():
    """With no explicit config, the default 100 MiB cap rejects an over-limit
    declared Content-Length cheaply (DoS protection is on by default)."""

    app = _app(max_size=None)  # no explicit config -> default 100 MiB
    req = Request(
        method="POST",
        path="/echo",
        query_string="",
        headers={
            "content-length": str(200 * 1024 * 1024),
            "content-type": "application/octet-stream",
        },
        body=b"",
    )
    resp = await app.handle_request(req)
    assert resp.status_code == 413


async def test_declared_content_length_over_limit_rejected_cheaply():
    """A liar that claims Content-Length over the limit is rejected without
    reading the body. We can't easily fake a content-length-vs-body mismatch
    through the TestClient — but we can construct a Request directly."""

    app = _app(max_size=100)

    req = Request(
        method="POST",
        path="/echo",
        query_string="",
        headers={"content-length": "999999", "content-type": "application/octet-stream"},
        body=b"",  # actual body shorter — irrelevant; declared length is the trigger
    )
    resp = await app.handle_request(req)
    assert resp.status_code == 413


async def test_actual_body_over_limit_rejected_even_without_content_length():
    """No Content-Length header but oversized body → still rejected."""

    app = _app(max_size=100)
    req = Request(
        method="POST",
        path="/echo",
        query_string="",
        headers={},
        body=b"x" * 500,
    )
    resp = await app.handle_request(req)
    assert resp.status_code == 413


async def test_incremental_limit_rejects_chunked_body_mid_stream():
    """A chunked body that omits Content-Length is rejected once the
    running total crosses MAX_CONTENT_LENGTH — caught while still being
    received, before the whole payload is buffered."""
    app = _app(max_size=100)

    chunks = [
        {"type": "http.request", "body": b"x" * 60, "more_body": True},
        {"type": "http.request", "body": b"x" * 60, "more_body": True},
        {"type": "http.request", "body": b"x" * 60, "more_body": False},
    ]
    pending = iter(chunks)
    sent: list[dict] = []

    async def receive() -> dict:
        return next(pending)

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/echo",
        "query_string": b"",
        "headers": [],  # deliberately no content-length
    }
    await app(scope, receive, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413
    # Rejected mid-stream: the third chunk was never consumed.
    assert next(pending) is chunks[2]


# ── The limit is enforced once, by whichever layer saw the bytes ──────


def _limited_app(limit: int) -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = limit

    @app.post("/u")
    async def upload(request: Request):
        return {"n": len(await request.body())}

    return app


async def _drive_without_content_length(app: Veloce, total: int, chunks: int) -> int:
    """POST `total` bytes in `chunks` messages, declaring no Content-Length."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/u",
        "raw_path": b"/u",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("t", 1),
        "server": ("t", 80),
    }
    per = total // chunks
    messages = iter(
        [
            {"type": "http.request", "body": b"x" * per, "more_body": index < chunks - 1}
            for index in range(chunks)
        ]
    )

    async def receive():
        return next(messages)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent[0]["status"]


def test_a_declared_over_limit_body_is_refused():
    with TestClient(_limited_app(100)) as client:
        assert client.post("/u", content=b"x" * 500).status_code == 413


def test_an_under_limit_body_is_accepted():
    with TestClient(_limited_app(100)) as client:
        assert client.post("/u", content=b"x" * 50).status_code == 200


async def test_an_undeclared_over_limit_body_is_still_refused_in_one_chunk():
    """Dispatch no longer re-checks, so the transport's own cap must hold."""
    assert await _drive_without_content_length(_limited_app(100), 500, 1) == 413


async def test_an_undeclared_over_limit_body_is_still_refused_across_chunks():
    """The running total is what catches a body that declares no length."""
    assert await _drive_without_content_length(_limited_app(100), 500, 5) == 413


async def test_an_undeclared_under_limit_body_is_accepted():
    assert await _drive_without_content_length(_limited_app(100), 50, 5) == 200


def test_a_mounted_sub_app_enforces_its_own_smaller_limit():
    """The sub-app is dispatched with a fresh request, so the parent's
    enforcement must not stand in for a limit the sub-app set lower."""
    parent = Veloce(openapi_url=None)
    parent.config["MAX_CONTENT_LENGTH"] = 10_000
    sub = _limited_app(50)
    parent.mount("/sub", sub)

    with TestClient(parent) as client:
        assert client.post("/sub/u", content=b"x" * 20).status_code == 200
        assert client.post("/sub/u", content=b"x" * 200).status_code == 413


def test_a_request_built_outside_a_transport_is_still_checked():
    """`handle_request` is public; a caller reaching it directly gets the check."""
    app = _limited_app(50)

    async def drive():
        request = Request(
            method="POST",
            path="/u",
            query_string="",
            headers={"host": "testserver"},
            body=b"x" * 200,
            app=app,
        )
        return await app.handle_request(request)

    assert asyncio.run(drive()).status_code == 413


# ── The declared-length scan compares the header as received ─────────
#
# ASGI mandates lowercase header names, so the scan matches the name as it
# arrives rather than lowercasing every header of every request to find one
# that is usually absent. What a spec-violating server loses is the *early*
# rejection, not the limit: the received length is enforced independently on
# both body branches. These drive the scope directly, because the test client
# builds a compliant scope and could not express the violation.


def _drive(app, headers: list[tuple[bytes, bytes]], body: bytes) -> int:
    """Send one POST through the raw ASGI surface and return its status."""
    status: list[int] = []

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/echo",
        "raw_path": b"/echo",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 5555),
        "server": ("127.0.0.1", 8000),
        "scheme": "http",
        "root_path": "",
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status.append(message["status"])

    asyncio.run(app(scope, receive, send))
    return status[0]


def test_a_compliant_declared_length_over_the_limit_is_refused():
    app = _app(max_size=16)
    headers = [(b"host", b"testserver"), (b"content-length", b"999999")]
    assert _drive(app, headers, b"x" * 999999) == 413


def test_an_oddly_cased_declared_length_still_has_its_body_refused():
    """The early rejection is lost; the limit is not."""
    app = _app(max_size=16)
    headers = [(b"host", b"testserver"), (b"Content-Length", b"999999")]
    assert _drive(app, headers, b"x" * 999999) == 413


def test_a_body_with_no_declared_length_at_all_is_still_refused():
    app = _app(max_size=16)
    assert _drive(app, [(b"host", b"testserver")], b"x" * 999999) == 413


def test_an_oddly_cased_declared_length_within_the_limit_is_served():
    app = _app(max_size=1024)
    headers = [(b"host", b"testserver"), (b"Content-Length", b"8")]
    assert _drive(app, headers, b"x" * 8) == 200
