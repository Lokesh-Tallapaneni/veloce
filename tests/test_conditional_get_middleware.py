"""Tests for ConditionalGetMiddleware."""

from __future__ import annotations

from veloce import (
    ConditionalGetMiddleware,
    GZipMiddleware,
    Response,
    StreamingResponse,
    TestClient,
    Veloce,
)


def test_synthesized_weak_etag_and_304():
    app = Veloce(openapi_url=None)
    app.add_middleware(ConditionalGetMiddleware())

    @app.get("/")
    async def index(request):
        return Response(body=b"hello")

    client = TestClient(app)
    r1 = client.get("/")
    etag = r1.headers.get("ETag") or r1.headers.get("etag")
    assert etag is not None
    assert etag.startswith('W/"')

    r2 = client.get("/", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.body in (b"", None)


def test_last_modified_drives_304():
    app = Veloce(openapi_url=None)
    app.add_middleware(ConditionalGetMiddleware())
    lm = "Wed, 21 Oct 2015 07:28:00 GMT"

    @app.get("/")
    async def index(request):
        return Response(body=b"x", headers={"Last-Modified": lm})

    client = TestClient(app)
    r = client.get("/", headers={"If-Modified-Since": lm})
    assert r.status_code == 304


def test_no_store_skips_synthesis():
    app = Veloce(openapi_url=None)
    app.add_middleware(ConditionalGetMiddleware())

    @app.get("/")
    async def index(request):
        return Response(body=b"x", headers={"Cache-Control": "no-store"})

    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert not (r.headers.get("ETag") or r.headers.get("etag"))


def test_auto_etag_false_still_forwards_handler_etag():
    app = Veloce(openapi_url=None)
    app.add_middleware(ConditionalGetMiddleware(auto_etag=False))

    @app.get("/")
    async def index(request):
        return Response(body=b"x", headers={"ETag": '"abc"'})

    client = TestClient(app)
    r1 = client.get("/")
    assert not r1.headers.get("ETag", "").startswith("W/") if r1.headers.get("ETag") else True
    r2 = client.get("/", headers={"If-None-Match": '"abc"'})
    assert r2.status_code == 304
    # No synthesis on a plain body.
    app2 = Veloce(openapi_url=None)
    app2.add_middleware(ConditionalGetMiddleware(auto_etag=False))

    @app2.get("/plain")
    async def plain(request):
        return Response(body=b"y")

    r3 = TestClient(app2).get("/plain")
    assert not (r3.headers.get("ETag") or r3.headers.get("etag"))


def test_post_untouched():
    app = Veloce(openapi_url=None)
    app.add_middleware(ConditionalGetMiddleware())

    @app.post("/")
    async def create(request):
        return Response(body=b"x", headers={"ETag": '"abc"'})

    client = TestClient(app)
    r = client.post("/", headers={"If-None-Match": '"abc"'})
    assert r.status_code == 200


def test_streaming_passes_through():
    app = Veloce(openapi_url=None)
    app.add_middleware(ConditionalGetMiddleware())

    @app.get("/")
    async def index(request):
        async def gen():
            yield b"chunk"

        return StreamingResponse(gen())

    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert not (r.headers.get("ETag") or r.headers.get("etag"))


def test_compose_with_gzip():
    app = Veloce(openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=0))
    app.add_middleware(ConditionalGetMiddleware())

    @app.get("/")
    async def index(request):
        return Response(body=b"hello world" * 10)

    client = TestClient(app)
    r = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200


def test_unit_method_gate_and_skips():
    import asyncio

    from veloce.http.request import Request

    mw = ConditionalGetMiddleware()

    def _req(method="GET"):
        return Request(method=method, path="/", query_string="", headers={}, body=b"")

    # POST gate
    resp = Response(body=b"x")
    out = asyncio.new_event_loop().run_until_complete(mw.process_response(_req("POST"), resp))
    assert out is resp
    assert not out.headers.get("ETag")
