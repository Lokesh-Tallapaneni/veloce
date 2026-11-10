"""StreamingResponse — ASGI chunked emit + sync-iterable support (Q38)."""

from __future__ import annotations

from veloce import JSONResponse, Request, StreamingResponse, Veloce
from veloce.testclient import TestClient


def test_async_generator_streamed_through_asgi():
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s(request: Request):
        async def gen():
            for i in range(3):
                yield f"chunk{i}".encode()

        return StreamingResponse(gen(), content_type="text/plain")

    with TestClient(app) as client:
        resp = client.get("/s")

    assert resp.status_code == 200
    assert resp.body == b"chunk0chunk1chunk2"


def test_bare_async_generator_return_value_is_streamed():
    """A handler returning an async generator directly, not wrapped in
    `StreamingResponse`, must stream its chunks rather than falling through
    to the JSON-encoding fallback (which previously serialised the
    generator object's own `repr()` as the response body)."""
    app = Veloce(openapi_url=None)

    @app.get("/s")
    def s():
        async def gen():
            yield b"hello "
            yield b"world"

        return gen()

    with TestClient(app) as client:
        resp = client.get("/s")

    assert resp.status_code == 200
    assert resp.body == b"hello world"


def test_bare_async_generator_return_streams_even_with_a_response_class_override():
    """The same bug as `test_bare_async_generator_return_value_is_streamed`,
    but with an explicit `response_class` set - no `Response` subclass can
    give a generator a shape of its own, so streaming must win regardless."""
    app = Veloce(openapi_url=None)

    @app.get("/s", response_class=JSONResponse)
    async def s():
        async def gen():
            yield b"hello "
            yield b"world"

        return gen()

    with TestClient(app) as client:
        resp = client.get("/s")

    assert resp.status_code == 200
    assert resp.body == b"hello world"


def test_sync_generator_accepted():
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s(request: Request):
        def gen():
            for i in range(3):
                yield f"line{i}\n"

        return StreamingResponse(gen(), content_type="text/plain")

    with TestClient(app) as client:
        resp = client.get("/s")

    assert resp.body == b"line0\nline1\nline2\n"


def test_sync_list_accepted():
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s(request: Request):
        return StreamingResponse([b"a", b"b", b"c"])

    with TestClient(app) as client:
        resp = client.get("/s")

    assert resp.body == b"abc"


def test_streaming_response_content_type_preserved():
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s(request: Request):
        async def gen():
            yield b"data"

        return StreamingResponse(gen(), content_type="text/csv")

    with TestClient(app) as client:
        resp = client.get("/s")

    assert resp.headers.get("content-type") == "text/csv"


def test_streaming_response_no_content_length_header():
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s(request: Request):
        async def gen():
            yield b"x"

        return StreamingResponse(gen())

    with TestClient(app) as client:
        resp = client.get("/s")

    # A streaming response is framed by the server, not Content-Length.
    assert "content-length" not in {k.lower() for k in resp.headers}


def test_streaming_response_custom_headers_emitted():
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s(request: Request):
        async def gen():
            yield b"x"

        return StreamingResponse(gen(), headers={"X-Stream": "yes"})

    with TestClient(app) as client:
        resp = client.get("/s")

    assert resp.headers.get("x-stream") == "yes"
