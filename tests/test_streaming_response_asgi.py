"""StreamingResponse — ASGI chunked emit + sync-iterable support (Q38)."""

from __future__ import annotations

from veloce import Request, StreamingResponse, Veloce
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
