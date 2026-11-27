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

from tests._asgi_drive import body_of, drive, http_scope, status_of
from veloce import Veloce


async def test_stream_route_receives_chunks_incrementally():
    app = Veloce()

    @app.post("/upload", stream=True)
    async def upload(request):
        chunks = [c async for c in request.stream()]
        return {"count": len(chunks), "body": b"".join(chunks).decode()}

    sent = await drive(app, http_scope(method="POST", path="/upload"), chunks=[b"aa", b"bb", b"cc"])
    assert status_of(sent) == 200
    # Three receive messages -> three streamed chunks (not one buffered slice).
    assert json.loads(body_of(sent)) == {"count": 3, "body": "aabbcc"}


async def test_stream_route_is_not_predrained():
    app = Veloce()

    @app.post("/s", stream=True)
    async def s(request):
        return {"drained_at_entry": request._body_drained}

    sent = await drive(app, http_scope(method="POST", path="/s"), chunks=[b"x", b"y"])
    assert json.loads(body_of(sent)) == {"drained_at_entry": False}


async def test_default_route_buffers_body_so_sync_accessor_works():
    app = Veloce()

    @app.post("/echo")
    async def echo(request):
        # Sync accessor: works only because the default route is
        # buffered before the handler, even though the body arrived in chunks.
        return {"drained": request._body_drained, "data": request.get_json()}

    headers = [(b"host", b"testserver"), (b"content-type", b"application/json")]
    sent = await drive(
        app,
        http_scope(method="POST", path="/echo", headers=headers),
        chunks=[b'{"a":', b" 1}"],
    )
    assert json.loads(body_of(sent)) == {"drained": True, "data": {"a": 1}}


async def test_stream_route_enforces_max_content_length():
    app = Veloce()
    app.config["MAX_CONTENT_LENGTH"] = 4

    @app.post("/big", stream=True)
    async def big(request):
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        return {"total": total}

    sent = await drive(app, http_scope(method="POST", path="/big"), chunks=[b"aa", b"bb", b"cc"])
    assert status_of(sent) == 413
