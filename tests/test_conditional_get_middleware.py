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


def test_mixedcase_no_store_skips_synthesis():
    # Field names are case-insensitive (RFC 9110 Sec. 5.1): a handler that set
    # `cache-control: no-store` in non-canonical casing must still suppress
    # auto-ETag synthesis.
    app = Veloce(openapi_url=None)
    app.add_middleware(ConditionalGetMiddleware())

    @app.get("/")
    async def index(request):
        return Response(body=b"x", headers={"cache-control": "no-store"})

    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert not (r.headers.get("ETag") or r.headers.get("etag"))


def test_mixedcase_etag_honored_for_304():
    # A handler-set `Etag` (non-canonical casing) must drive the 304 downgrade
    # on a matching If-None-Match, just as a canonical `ETag` would.
    app = Veloce(openapi_url=None)
    app.add_middleware(ConditionalGetMiddleware())

    @app.get("/")
    async def index(request):
        return Response(body=b"x", headers={"Etag": '"abc"'})

    client = TestClient(app)
    r = client.get("/", headers={"If-None-Match": '"abc"'})
    assert r.status_code == 304


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


def test_streaming_with_etag_not_downgraded_to_304():
    # A StreamingResponse carrying its own ETag must NOT be downgraded to a
    # bodiless 304 on a matching If-None-Match: make_conditional() clears `body`
    # but not `_stream`, so a 304 would still emit the chunks - protocol-invalid
    # per RFC 9110 Sec. 15.4.5. The stream passes through unchanged (200).
    app = Veloce(openapi_url=None)
    app.add_middleware(ConditionalGetMiddleware())

    @app.get("/")
    async def index(request):
        async def gen():
            yield b"chunk-"
            yield b"data"

        return StreamingResponse(gen(), headers={"ETag": '"stream-tag"'})

    client = TestClient(app)
    r = client.get("/", headers={"If-None-Match": '"stream-tag"'})
    assert r.status_code == 200
    assert r.body == b"chunk-data"


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


async def test_unit_method_gate_and_skips():

    from veloce.http.request import Request

    mw = ConditionalGetMiddleware()

    def _req(method="GET"):
        return Request(method=method, path="/", query_string="", headers={}, body=b"")

    # POST gate
    resp = Response(body=b"x")
    out = await mw.process_response(_req("POST"), resp)
    assert out is resp
    assert not out.headers.get("ETag")


# ── The ASGI emit path agrees with the native one ────────────────────


def _etag_app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(ConditionalGetMiddleware())

    @app.get("/r")
    async def resource():
        return {"a": "x" * 40}

    return app


def test_a_304_advertises_the_representation_length_over_asgi():
    """The 200 advertised 48 bytes and the 304 advertised 0 for the same entity."""
    with TestClient(_etag_app()) as client:
        first = client.get("/r")
        assert first.headers["content-length"] == "48"
        second = client.get("/r", headers={"If-None-Match": first.headers["etag"]})
        assert second.status_code == 304
        assert second.headers["content-length"] == "48"


def test_a_304_over_asgi_sends_no_body():
    with TestClient(_etag_app()) as client:
        etag = client.get("/r").headers["etag"]
        assert client.get("/r", headers={"If-None-Match": etag}).body == b""


def test_a_304_over_asgi_still_carries_its_validator():
    with TestClient(_etag_app()) as client:
        etag = client.get("/r").headers["etag"]
        assert client.get("/r", headers={"If-None-Match": etag}).headers["etag"] == etag


def test_a_head_response_still_advertises_the_get_length():
    """HEAD shares the branch the 304 length is computed on."""
    app = Veloce(openapi_url=None)

    @app.get("/r")
    async def resource():
        return {"a": "x" * 40}

    with TestClient(app) as client:
        assert client.head("/r").headers["content-length"] == "48"
