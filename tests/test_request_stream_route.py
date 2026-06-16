"""Opt-in request-body streaming on the ASGI path.

A `stream=True` route is dispatched WITHOUT the body being buffered first, so
its handler can consume `request.stream()` chunk-by-chunk as the ASGI server
delivers them. Every other route is buffered before the handler (the default),
so the synchronous body accessors keep working. These tests drive the ASGI
callable directly with a multi-message `receive` because the in-memory
TestClient delivers the whole body in one chunk and so cannot exercise
incremental delivery.
"""

from __future__ import annotations

import json

from veloce import Veloce


def _scope(method: str, path: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": headers or [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
    }


async def _drive(app: Veloce, scope: dict, chunks: list[bytes]) -> list[dict]:
    messages = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    msg_iter = iter(messages)

    async def receive() -> dict:
        try:
            return next(msg_iter)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _resp_body(sent: list[dict]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def _resp_status(sent: list[dict]) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def test_stream_route_receives_chunks_incrementally():
    app = Veloce()

    @app.post("/upload", stream=True)
    async def upload(request):
        chunks = [c async for c in request.stream()]
        return {"count": len(chunks), "body": b"".join(chunks).decode()}

    sent = await _drive(app, _scope("POST", "/upload"), [b"aa", b"bb", b"cc"])
    assert _resp_status(sent) == 200
    # Three receive messages -> three streamed chunks (not one buffered slice).
    assert json.loads(_resp_body(sent)) == {"count": 3, "body": "aabbcc"}


async def test_stream_route_is_not_predrained():
    app = Veloce()

    @app.post("/s", stream=True)
    async def s(request):
        return {"drained_at_entry": request._body_drained}

    sent = await _drive(app, _scope("POST", "/s"), [b"x", b"y"])
    assert json.loads(_resp_body(sent)) == {"drained_at_entry": False}


async def test_default_route_buffers_body_so_sync_accessor_works():
    app = Veloce()

    @app.post("/echo")
    async def echo(request):
        # Sync Flask-style accessor: works only because the default route is
        # buffered before the handler, even though the body arrived in chunks.
        return {"drained": request._body_drained, "data": request.get_json()}

    headers = [(b"host", b"testserver"), (b"content-type", b"application/json")]
    sent = await _drive(app, _scope("POST", "/echo", headers), [b'{"a":', b" 1}"])
    assert json.loads(_resp_body(sent)) == {"drained": True, "data": {"a": 1}}


async def test_stream_route_enforces_max_content_length():
    app = Veloce()
    app.config["MAX_CONTENT_LENGTH"] = 4

    @app.post("/big", stream=True)
    async def big(request):
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        return {"total": total}

    sent = await _drive(app, _scope("POST", "/big"), [b"aa", b"bb", b"cc"])
    assert _resp_status(sent) == 413
