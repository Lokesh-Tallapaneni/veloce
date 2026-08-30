"""Content-coding negotiation across zstd, brotli and gzip.

`GZipMiddleware` compressed with gzip and negotiated only gzip, so a browser
sending `Accept-Encoding: gzip, deflate, br, zstd` - which every current browser
does - was served the oldest coding it offered.

`CompressionMiddleware` offers several and picks one. `GZipMiddleware` remains,
pinned to gzip, so an existing stack behaves exactly as before.

Selection is the client's preference first, then the server's. A client's
q-values order what it wants (RFC 9110 Sec. 12.5.3); among equals the server's
`algorithms` order decides, so a deployment can prefer ratio or speed without the
client having to ask. `q=0` is a refusal, `identity;q=0` means the client will not
take an uncompressed body, and `*` stands for anything not named.

The codecs are optional. Brotli and zstd each need a third-party package, so an
algorithm whose package is missing is dropped at construction rather than failing
per request - unless it is the only one asked for, which is a misconfiguration
worth an actionable error at startup instead of silent plaintext.

Levels are per codec because their scales do not correspond: gzip is 1-9, brotli
quality 0-11, zstd 1-22. The brotli default here is deliberately not its library
default of 11, which is a ratio-at-any-cost setting far too slow to sit on a
dynamic response path.
"""

from __future__ import annotations

import ast
import gzip
import gzip as gzip_module
import inspect
import textwrap

import pytest

from veloce import CompressionMiddleware, GZipMiddleware, Response, StreamingResponse, Veloce
from veloce.testclient import TestClient

brotli = pytest.importorskip("brotli", reason="brotli is not installed")
zstandard = pytest.importorskip("zstandard", reason="zstandard is not installed")

BODY = b"veloce compression negotiation " * 200


def _app(middleware) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(middleware)

    @app.get("/text")
    async def text():
        return Response(body=BODY, content_type="text/plain")

    @app.get("/stream")
    async def stream():
        async def gen():
            for _ in range(40):
                yield b"veloce streaming payload "

        return StreamingResponse(gen(), content_type="text/plain")

    return app


def _decode(encoding: str, payload: bytes) -> bytes:
    if encoding == "gzip":
        return gzip_module.decompress(payload)
    if encoding == "br":
        return brotli.decompress(payload)
    if encoding == "zstd":
        return zstandard.ZstdDecompressor().decompressobj().decompress(payload)
    raise AssertionError(f"unexpected encoding {encoding!r}")


def _get(app: Veloce, accept: str | None, path: str = "/text"):
    headers = {} if accept is None else {"Accept-Encoding": accept}
    return TestClient(app).get(path, headers=headers)


# ── the negotiation itself ───────────────────────────────────────────


@pytest.mark.parametrize("coding", ["gzip", "br", "zstd"])
def test_each_offered_coding_can_be_selected(coding: str):
    response = _get(_app(CompressionMiddleware()), coding)
    assert response.headers["Content-Encoding"] == coding
    assert _decode(coding, response.body) == BODY


def test_the_server_order_decides_among_equally_acceptable_codings():
    """A bare list carries no preference, so the server's order applies."""
    response = _get(
        _app(CompressionMiddleware(algorithms=("zstd", "br", "gzip"))), "gzip, br, zstd"
    )
    assert response.headers["Content-Encoding"] == "zstd"


def test_a_different_server_order_selects_differently():
    """The same request, the other deployment preference."""
    response = _get(_app(CompressionMiddleware(algorithms=("gzip", "br"))), "gzip, br, zstd")
    assert response.headers["Content-Encoding"] == "gzip"


def test_a_client_q_value_outranks_the_server_order():
    """The client asked for brotli more strongly; the server prefers zstd."""
    middleware = CompressionMiddleware(algorithms=("zstd", "br", "gzip"))
    response = _get(_app(middleware), "zstd;q=0.1, br;q=0.9")
    assert response.headers["Content-Encoding"] == "br"


def test_the_highest_q_wins_across_three_codings():
    middleware = CompressionMiddleware(algorithms=("zstd", "br", "gzip"))
    response = _get(_app(middleware), "zstd;q=0.2, br;q=0.3, gzip;q=0.9")
    assert response.headers["Content-Encoding"] == "gzip"


def test_a_refused_coding_is_not_selected():
    middleware = CompressionMiddleware(algorithms=("zstd", "br", "gzip"))
    response = _get(_app(middleware), "zstd;q=0, br;q=0, gzip")
    assert response.headers["Content-Encoding"] == "gzip"


def test_every_coding_refused_leaves_the_body_alone():
    middleware = CompressionMiddleware(algorithms=("zstd", "br", "gzip"))
    response = _get(_app(middleware), "zstd;q=0, br;q=0, gzip;q=0")
    assert "Content-Encoding" not in response.headers
    assert response.body == BODY


def test_a_wildcard_selects_the_server_preference():
    middleware = CompressionMiddleware(algorithms=("br", "gzip"))
    assert _get(_app(middleware), "*").headers["Content-Encoding"] == "br"


def test_a_wildcard_refusal_blocks_unnamed_codings():
    """`*;q=0` refuses everything not explicitly listed."""
    middleware = CompressionMiddleware(algorithms=("zstd", "br", "gzip"))
    response = _get(_app(middleware), "gzip, *;q=0")
    assert response.headers["Content-Encoding"] == "gzip"


def test_an_unknown_coding_is_ignored():
    middleware = CompressionMiddleware(algorithms=("gzip",))
    assert _get(_app(middleware), "exotic-coding, gzip").headers["Content-Encoding"] == "gzip"


def test_no_accept_encoding_header_leaves_the_body_alone():
    response = _get(_app(CompressionMiddleware()), None)
    assert "Content-Encoding" not in response.headers
    assert response.body == BODY


def test_an_empty_accept_encoding_leaves_the_body_alone():
    response = _get(_app(CompressionMiddleware()), "")
    assert "Content-Encoding" not in response.headers


def test_casing_and_spacing_are_tolerated():
    response = _get(_app(CompressionMiddleware(algorithms=("br", "gzip"))), "  BR ;Q=1.0 ")
    assert response.headers["Content-Encoding"] == "br"


# ── every response varies on the header it negotiated ────────────────


@pytest.mark.parametrize("accept", ["gzip", "br", "zstd", "", "identity"])
def test_vary_is_declared_whether_or_not_it_compressed(accept: str):
    """A cache that missed this would serve one client's coding to another."""
    response = _get(_app(CompressionMiddleware()), accept)
    assert "accept-encoding" in response.headers.get("Vary", "").lower()


# ── the configured algorithm set ─────────────────────────────────────


def test_algorithms_defaults_to_all_available():
    assert set(CompressionMiddleware().algorithms) == {"zstd", "br", "gzip"}


def test_an_explicit_order_is_preserved():
    assert CompressionMiddleware(algorithms=("gzip", "br")).algorithms == ("gzip", "br")


def test_an_unavailable_algorithm_is_dropped(monkeypatch):
    """A missing package must not fail every response."""
    import veloce.middleware.compression as compression

    monkeypatch.delitem(compression._CODECS, "br")
    assert "br" not in CompressionMiddleware(algorithms=("br", "gzip")).algorithms


def test_asking_only_for_an_unavailable_algorithm_raises(monkeypatch):
    """Silently serving plaintext would hide the misconfiguration."""
    import veloce.middleware.compression as compression

    monkeypatch.delitem(compression._CODECS, "br")
    with pytest.raises(ValueError, match="brotli"):
        CompressionMiddleware(algorithms=("br",))


def test_an_unknown_algorithm_name_raises():
    with pytest.raises(ValueError, match="deflate"):
        CompressionMiddleware(algorithms=("deflate",))


def test_an_empty_algorithm_tuple_raises():
    with pytest.raises(ValueError):
        CompressionMiddleware(algorithms=())


# ── levels are per codec ─────────────────────────────────────────────


def test_a_per_codec_level_is_accepted():
    middleware = CompressionMiddleware(algorithms=("br",), levels={"br": 1})
    assert _decode("br", _get(_app(middleware), "br").body) == BODY


def test_the_brotli_default_is_not_the_library_maximum():
    """Quality 11 is a ratio-at-any-cost setting, far too slow to serve from."""
    assert CompressionMiddleware().levels["br"] <= 6


def test_a_level_for_an_unknown_codec_raises():
    with pytest.raises(ValueError, match="deflate"):
        CompressionMiddleware(levels={"deflate": 1})


# ── the existing gzip middleware is unchanged ────────────────────────


def test_gzip_middleware_still_offers_only_gzip():
    assert GZipMiddleware().algorithms == ("gzip",)


def test_gzip_middleware_ignores_a_brotli_request():
    response = _get(_app(GZipMiddleware(minimum_size=1)), "br")
    assert "Content-Encoding" not in response.headers


def test_gzip_middleware_still_compresses_gzip():
    response = _get(_app(GZipMiddleware(minimum_size=1)), "gzip")
    assert response.headers["Content-Encoding"] == "gzip"
    assert gzip_module.decompress(response.body) == BODY


def test_gzip_middleware_keeps_its_constructor():
    """An existing stack must keep working with the arguments it already passes."""
    middleware = GZipMiddleware(minimum_size=1024, compresslevel=9)
    assert middleware.minimum_size == 1024
    assert middleware.levels["gzip"] == 9


# ── the guards that applied to gzip apply to every coding ────────────


@pytest.mark.parametrize("coding", ["gzip", "br", "zstd"])
def test_a_body_below_the_threshold_is_not_compressed(coding: str):
    app = Veloce(openapi_url=None)
    app.add_middleware(CompressionMiddleware(minimum_size=10_000))

    @app.get("/small")
    async def small():
        return Response(body=b"tiny", content_type="text/plain")

    response = TestClient(app).get("/small", headers={"Accept-Encoding": coding})
    assert "Content-Encoding" not in response.headers


@pytest.mark.parametrize("coding", ["gzip", "br", "zstd"])
def test_an_incompressible_type_is_left_alone(coding: str):
    app = Veloce(openapi_url=None)
    app.add_middleware(CompressionMiddleware(minimum_size=1))

    @app.get("/img")
    async def img():
        return Response(body=BODY, content_type="image/png")

    response = TestClient(app).get("/img", headers={"Accept-Encoding": coding})
    assert "Content-Encoding" not in response.headers


@pytest.mark.parametrize("coding", ["gzip", "br", "zstd"])
def test_an_already_encoded_body_is_not_re_encoded(coding: str):
    app = Veloce(openapi_url=None)
    app.add_middleware(CompressionMiddleware(minimum_size=1))

    @app.get("/pre")
    async def pre():
        return Response(
            body=BODY, content_type="text/plain", headers={"Content-Encoding": "identity-ish"}
        )

    response = TestClient(app).get("/pre", headers={"Accept-Encoding": coding})
    assert response.headers["Content-Encoding"] == "identity-ish"


@pytest.mark.parametrize("coding", ["gzip", "br", "zstd"])
def test_the_content_length_describes_the_compressed_body(coding: str):
    response = _get(_app(CompressionMiddleware(minimum_size=1)), coding)
    assert int(response.headers["Content-Length"]) == len(response.body)


@pytest.mark.parametrize("coding", ["gzip", "br", "zstd"])
def test_a_strong_etag_is_weakened(coding: str):
    """The bytes changed, so a strong validator would be a lie."""
    app = Veloce(openapi_url=None)
    app.add_middleware(CompressionMiddleware(minimum_size=1))

    @app.get("/tagged")
    async def tagged():
        return Response(body=BODY, content_type="text/plain", headers={"ETag": '"abc"'})

    response = TestClient(app).get("/tagged", headers={"Accept-Encoding": coding})
    assert response.headers["ETag"].startswith("W/")


# ── streaming ────────────────────────────────────────────────────────


@pytest.mark.parametrize("coding", ["gzip", "br", "zstd"])
def test_a_streamed_body_round_trips(coding: str):
    response = _get(_app(CompressionMiddleware(minimum_size=1)), coding, path="/stream")
    assert response.headers["Content-Encoding"] == coding
    assert _decode(coding, response.body) == b"veloce streaming payload " * 40


@pytest.mark.parametrize("coding", ["gzip", "br", "zstd"])
def test_a_refused_stream_is_served_plain(coding: str):
    response = _get(_app(CompressionMiddleware(minimum_size=1)), f"{coding};q=0", path="/stream")
    assert response.headers.get("Content-Encoding") != coding


def test_an_event_stream_is_never_compressed():
    """Buffering through a compressor would merge and delay events."""
    app = Veloce(openapi_url=None)
    app.add_middleware(CompressionMiddleware(minimum_size=1))

    @app.get("/sse")
    async def sse():
        async def gen():
            yield b"data: one\n\n"

        return StreamingResponse(gen(), content_type="text/event-stream")

    response = TestClient(app).get("/sse", headers={"Accept-Encoding": "br"})
    assert "Content-Encoding" not in response.headers


# ── end to end ───────────────────────────────────────────────────────


def test_two_clients_get_the_coding_each_asked_for():
    app = _app(CompressionMiddleware(algorithms=("zstd", "br", "gzip")))
    client = TestClient(app)
    first = client.get("/text", headers={"Accept-Encoding": "gzip"})
    second = client.get("/text", headers={"Accept-Encoding": "br"})
    assert first.headers["Content-Encoding"] == "gzip"
    assert second.headers["Content-Encoding"] == "br"
    assert _decode("gzip", first.body) == _decode("br", second.body) == BODY


def test_repeated_requests_are_stable():
    app = _app(CompressionMiddleware())
    client = TestClient(app)
    for _ in range(10):
        response = client.get("/text", headers={"Accept-Encoding": "br"})
        assert _decode("br", response.body) == BODY


def test_a_browser_shaped_header_selects_a_modern_coding():
    """The header every current browser actually sends."""
    middleware = CompressionMiddleware(algorithms=("zstd", "br", "gzip"))
    response = _get(_app(middleware), "gzip, deflate, br, zstd")
    assert response.headers["Content-Encoding"] == "zstd"


# ── compress_stream carries no unused parameter ──────────────────
#
# Moved here from `test_unswept_scope_findings.py`, a module named for the audit
# batch that produced it rather than for the source it covers.


def test_compress_stream_carries_no_unused_parameter():
    """The original finding was a `request` argument threaded through and never
    read. `coding` was added later and is read, so the check is that every
    parameter appears in the body rather than that the signature never grows.
    """
    from veloce.middleware.compression import CompressionMiddleware

    function = CompressionMiddleware._compress_stream

    # Names the body actually loads. This split the source on the first colon,
    # which for `_compress_stream(self, stream: Any, coding: str)` falls inside
    # the signature - so every parameter matched its own annotation, or the
    # docstring, and a never-read argument passed. Parsing keeps the annotations
    # and the docstring out of it: a `str` constant contributes no `Name`.
    definition = ast.parse(textwrap.dedent(inspect.getsource(function))).body[0]
    read = {
        node.id
        for statement in definition.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }

    params = [p for p in inspect.signature(function).parameters][1:]
    assert params
    assert "request" not in params
    for param in params:
        assert param in read, f"{param} is never read"


def test_streaming_compression_still_works():
    """The negative: removing the argument must not disturb the path."""
    app = Veloce(openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=1))

    @app.get("/s")
    async def s():
        async def gen():
            for _ in range(50):
                yield b"hello world "

        return StreamingResponse(gen(), content_type="text/plain")

    response = TestClient(app).get("/s", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("Content-Encoding") == "gzip"
    # The client does not decode for us, so the round trip is the assertion.
    assert gzip.decompress(response.body) == b"hello world " * 50


def test_a_refused_encoding_still_streams_plain():
    app = Veloce(openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=1))

    @app.get("/s")
    async def s():
        async def gen():
            yield b"plain"

        return StreamingResponse(gen(), content_type="text/plain")

    response = TestClient(app).get("/s", headers={"Accept-Encoding": "gzip;q=0"})
    assert response.headers.get("Content-Encoding") != "gzip"
    assert response.text == "plain"


# ── gzip: an already-encoded body is not re-encoded ──────────────────
#
# Moved here from `test_route_metadata.py`, which is about route-level
# OpenAPI metadata and held these only because the two landed in one batch.


def _make_gzip_app() -> Veloce:
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=10))

    @app.get("/plain")
    async def plain():
        return "x" * 2000

    @app.get("/pre-encoded")
    async def pre_encoded():
        # A handler that returns a body that's already been compressed
        # (and declares it via Content-Encoding) — middleware must NOT
        # re-gzip it, or no client can decode the result.
        # A *compressible* content type on purpose. With
        # `application/octet-stream` the middleware skipped on type and never
        # reached the Content-Encoding check at all.
        body = b"x" * 2000
        compressed = gzip.compress(body)
        return Response(
            body=compressed,
            content_type="text/plain",
            headers={"Content-Encoding": "gzip"},
        )

    @app.get("/identity-encoded")
    async def identity_encoded():
        # `Content-Encoding: identity` means "no encoding" — the middleware
        # should treat this as if no encoding were declared and proceed.
        return Response(
            body=b"x" * 2000,
            content_type="text/plain",
            headers={"Content-Encoding": "identity"},
        )

    return app


def test_gzip_compresses_when_no_existing_encoding():
    client = TestClient(_make_gzip_app())
    resp = client.get("/plain", headers={"accept-encoding": "gzip"})
    assert resp.status_code == 200
    # Body should be gzipped now.
    assert resp.headers.get("content-encoding") == "gzip"
    # Vary header carries Accept-Encoding so caches key correctly.
    vary = resp.headers.get("vary", "").lower()
    assert "accept-encoding" in vary


def test_gzip_skips_already_encoded_response():
    """A genuinely gzipped body reaches the client decodable in one pass.

    The end-to-end half: `test_an_already_encoded_body_is_not_re_encoded` in
    `test_compression_negotiation.py` isolates the Content-Encoding check with
    a placeholder token, and a mutation deleting that check fails it. It cannot
    fail here - a second gzip layer over real gzip output is *larger* than the
    first, so the `clen < len(body)` guard declines it anyway. This asserts the
    outcome a client depends on instead of claiming to pin the branch.
    """
    client = TestClient(_make_gzip_app())
    resp = client.get("/pre-encoded", headers={"accept-encoding": "gzip"})
    assert resp.status_code == 200
    # Both halves of "did not add a second layer": the header still declares
    # one encoding rather than `gzip, gzip`, and one decode returns the
    # original. Decoding alone leaves a doubled header undetected, and a
    # doubled header is what tells a client how many times to decode.
    assert resp.headers.get("content-encoding") == "gzip"
    assert gzip.decompress(resp.body) == b"x" * 2000


def test_gzip_proceeds_when_existing_encoding_is_identity():
    """`Content-Encoding: identity` is a no-op declaration; treat as no encoding."""
    client = TestClient(_make_gzip_app())
    resp = client.get("/identity-encoded", headers={"accept-encoding": "gzip"})
    # Middleware should have replaced `identity` with `gzip`.
    assert resp.headers.get("content-encoding") == "gzip"
    assert gzip.decompress(resp.body) == b"x" * 2000


def test_gzip_skipped_without_accept_encoding():
    client = TestClient(_make_gzip_app())
    resp = client.get("/plain")  # no accept-encoding
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") is None
    assert resp.body == b"x" * 2000


def test_gzip_skipped_below_minimum_size():
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=10_000))

    @app.get("/tiny")
    async def tiny():
        return "x"

    resp = TestClient(app).get("/tiny", headers={"accept-encoding": "gzip"})
    # Not compressed, *and* delivered intact: a middleware that dropped the
    # body below the threshold would satisfy the header assertion alone.
    assert resp.headers.get("content-encoding") is None
    assert resp.body == b"x"


def test_gzip_vary_header_appended_not_replaced():
    """If the response already had a Vary header, Accept-Encoding is appended."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=10))

    @app.get("/x")
    async def x():
        return Response(
            body=b"x" * 2000,
            content_type="text/plain",
            headers={"Vary": "Origin"},
        )

    resp = TestClient(app).get("/x", headers={"accept-encoding": "gzip"})
    vary = resp.headers.get("vary", "")
    assert "Origin" in vary
    assert "Accept-Encoding" in vary
