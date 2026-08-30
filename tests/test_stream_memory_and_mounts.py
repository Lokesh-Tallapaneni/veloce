"""`stream=True` streams, and holds nothing while it does.

Two defects made the flag a promise the framework did not keep.

`Request.stream()` accumulated every chunk it yielded into `_body`, so a later
`request.body()` could serve the payload - which meant a `stream=True` route had
exactly the memory profile of a buffered one. 32 MiB uploaded and consumed left
33.6 MB retained. An app that added the flag to bound memory under large uploads
was not bounded.

And the flag stopped applying once the app was mounted. A mounted path matches
no route in the parent, so the transport's eager drain ran before anything could
ask whether the sub-app's route streams. Nothing errored - the response is
identical either way, which is why it went unnoticed - the route just quietly
lost the property it asked for, and only when composed into a parent.
"""

from __future__ import annotations

import tracemalloc

import orjson
import pytest

from tests._asgi_drive import http_scope
from veloce import Veloce

_CHUNK = 1024 * 1024
_CHUNKS = 16


def _sink_app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = None

    async def sink(request):
        sizes = []
        async for chunk in request.stream():
            sizes.append(len(chunk))
        return {"chunks": sizes}

    app.add_route("/u", sink, methods=["POST"], stream=True)

    @app.post("/b")
    async def buffered(request):
        return {"n": len(await request.body())}

    return app


async def _drive(app: Veloce, path: str, chunk: bytes, count: int) -> dict:
    scope = http_scope(
        type="http",
        method="POST",
        path=path,
        query_string=b"",
        headers=[],
        root_path="",
        scheme="http",
        http_version="1.1",
    )
    sent = 0

    async def receive() -> dict:
        nonlocal sent
        if sent < count:
            sent += 1
            return {"type": "http.request", "body": chunk, "more_body": sent < count}
        return {"type": "http.request", "body": b"", "more_body": False}

    out: list[dict] = []

    async def send(message: dict) -> None:
        out.append(message)

    await app(scope, receive, send)
    return orjson.loads(b"".join(m.get("body", b"") for m in out[1:]))


# ── Streaming holds nothing ──────────────────────────────────────────


async def test_a_streamed_body_is_not_retained():
    """The defect: the whole upload stayed resident after the handler read it."""
    app = _sink_app()
    tracemalloc.start()
    try:
        result = await _drive(app, "/u", b"x" * _CHUNK, _CHUNKS)
        retained, _peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert sum(result["chunks"]) == _CHUNK * _CHUNKS
    # Retention must not scale with the upload. A generous ceiling: a couple of
    # chunks in flight, not the whole body.
    assert retained < _CHUNK * 4, f"retained {retained / 1e6:.1f} MB of a 16 MiB upload"


async def test_a_streamed_body_still_reaches_the_handler_whole():
    result = await _drive(_sink_app(), "/u", b"0123456789", 3)
    assert result["chunks"] == [10, 10, 10]


async def test_a_buffered_route_is_unaffected():
    assert (await _drive(_sink_app(), "/b", b"0123456789", 3))["n"] == 30


# ── The flag survives being mounted ──────────────────────────────────


def _mounted() -> Veloce:
    parent = Veloce(openapi_url=None)
    parent.mount("/api", _sink_app())
    return parent


async def test_a_stream_route_still_streams_behind_a_mount():
    """The defect: it arrived as one buffered chunk once the app was mounted."""
    assert (await _drive(_mounted(), "/api/u", b"0123456789", 3))["chunks"] == [10, 10, 10]


async def test_mounted_and_direct_streaming_agree():
    direct = await _drive(_sink_app(), "/u", b"0123456789", 3)
    mounted = await _drive(_mounted(), "/api/u", b"0123456789", 3)
    assert direct == mounted


async def test_a_buffered_route_behind_a_mount_still_gets_its_whole_body():
    """Deferring the decision must not leave a buffered sub-route un-drained."""
    assert (await _drive(_mounted(), "/api/b", b"0123456789", 3))["n"] == 30


@pytest.mark.parametrize("path", ["/api/u", "/api/b"])
async def test_a_mounted_route_answers_normally(path):
    result = await _drive(_mounted(), path, b"0123456789", 3)
    assert result
