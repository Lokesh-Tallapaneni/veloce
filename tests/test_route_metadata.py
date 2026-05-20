"""Tests for route-level OpenAPI metadata + GZip skip (R26, M5)."""

from __future__ import annotations

import gzip

import pytest

from veloce import GZipMiddleware, Response, Veloce
from veloce.testclient import TestClient

# ── R26: operation_id ─────────────────────────────────────────────────


def test_operation_id_override_appears_in_openapi():
    app = Veloce(debug=True)

    @app.get("/items", operation_id="list_items_v1")
    async def list_items():
        return []

    client = TestClient(app)
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/items"]["get"]
    assert op["operationId"] == "list_items_v1"


def test_operation_id_defaults_to_name_underscore_method():
    """No override → fallback to `<name>_<method>` (one-time stable id)."""
    app = Veloce(debug=True)

    @app.get("/items")
    async def list_items():
        return []

    client = TestClient(app)
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/items"]["get"]
    assert op["operationId"] == "list_items_get"


def test_operation_id_works_via_route_decorator():
    """The generic `@router.route(...)` decorator also accepts operation_id."""
    app = Veloce(debug=True)

    @app.route("/x", methods=["POST"], operation_id="create_x_explicit")
    async def x():
        return {}

    spec = TestClient(app).get("/openapi.json").json()
    op = spec["paths"]["/x"]["post"]
    assert op["operationId"] == "create_x_explicit"


# ── M5: GZipMiddleware skips already-encoded responses ───────────────


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
        body = b"x" * 2000
        compressed = gzip.compress(body)
        return Response(
            body=compressed,
            content_type="application/octet-stream",
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
    client = TestClient(_make_gzip_app())
    resp = client.get("/pre-encoded", headers={"accept-encoding": "gzip"})
    assert resp.status_code == 200
    # The middleware must NOT have added a second `gzip` layer.
    # Decoding the body once with gzip should give back the original.
    decoded = gzip.decompress(resp.body)
    assert decoded == b"x" * 2000


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


def test_gzip_skipped_below_minimum_size():
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=10_000))

    @app.get("/tiny")
    async def tiny():
        return "x"

    resp = TestClient(app).get("/tiny", headers={"accept-encoding": "gzip"})
    assert resp.headers.get("content-encoding") is None


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


# ── pytest plugin sanity ─────────────────────────────────────────────


def test_pytest_imported():
    assert pytest is not None
