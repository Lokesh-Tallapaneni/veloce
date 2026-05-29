"""MAX_CONTENT_LENGTH → 413 enforcement (Q19)."""

from __future__ import annotations

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


def test_unset_max_content_length_allows_any_size():
    """When MAX_CONTENT_LENGTH isn't configured, large bodies pass through."""
    client = TestClient(_app(max_size=None))
    big = b"x" * 1_000_000
    resp = client.post("/echo", content=big)
    assert resp.status_code == 200
    assert resp.json() == {"received": 1_000_000}


def test_declared_content_length_over_limit_rejected_cheaply():
    """A liar that claims Content-Length over the limit is rejected without
    reading the body. We can't easily fake a content-length-vs-body mismatch
    through the TestClient — but we can construct a Request directly."""
    import asyncio

    app = _app(max_size=100)

    req = Request(
        method="POST",
        path="/echo",
        query_string="",
        headers={"content-length": "999999", "content-type": "application/octet-stream"},
        body=b"",  # actual body shorter — irrelevant; declared length is the trigger
    )
    resp = asyncio.new_event_loop().run_until_complete(app.handle_request(req))
    assert resp.status_code == 413


def test_actual_body_over_limit_rejected_even_without_content_length():
    """No Content-Length header but oversized body → still rejected."""
    import asyncio

    app = _app(max_size=100)
    req = Request(
        method="POST",
        path="/echo",
        query_string="",
        headers={},
        body=b"x" * 500,
    )
    resp = asyncio.new_event_loop().run_until_complete(app.handle_request(req))
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
