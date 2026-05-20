"""stream_with_context — keep the request context alive during streaming (Q39)."""

from __future__ import annotations

from veloce import Request, StreamingResponse, Veloce, g, request, stream_with_context
from veloce.testclient import TestClient


def test_generator_can_read_request_during_streaming():
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s(req: Request):
        async def gen():
            # `request` proxy resolves only if the context survived.
            for _ in range(3):
                yield request.path.encode()

        return StreamingResponse(stream_with_context(gen()), content_type="text/plain")

    with TestClient(app) as client:
        resp = client.get("/s")

    assert resp.status_code == 200
    assert resp.body == b"/s/s/s"


def test_generator_can_read_g_during_streaming():
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s(req: Request):
        g.token = "abc"

        async def gen():
            yield g.token.encode()

        return StreamingResponse(stream_with_context(gen()), content_type="text/plain")

    with TestClient(app) as client:
        resp = client.get("/s")

    assert resp.body == b"abc"


def test_sync_generator_wrapped():
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s(req: Request):
        def gen():
            for i in range(3):
                yield f"{request.path}{i}"

        return StreamingResponse(stream_with_context(gen()), content_type="text/plain")

    with TestClient(app) as client:
        resp = client.get("/s")

    assert resp.body == b"/s0/s1/s2"


def test_streamed_content_is_complete():
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s(req: Request):
        async def gen():
            for i in range(5):
                yield f"chunk{i};".encode()

        return StreamingResponse(stream_with_context(gen()))

    with TestClient(app) as client:
        resp = client.get("/s")

    assert resp.body == b"chunk0;chunk1;chunk2;chunk3;chunk4;"
