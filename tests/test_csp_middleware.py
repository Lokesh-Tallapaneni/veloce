"""Tests for CSPMiddleware and csp_nonce."""

from __future__ import annotations

import pytest

import veloce
from veloce import CSPMiddleware, Response, TestClient, Veloce, csp_nonce


def test_static_policy_verbatim():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(policy="default-src 'self'"))

    @app.get("/")
    async def index(request):
        return Response(body=b"x")

    r = TestClient(app).get("/")
    assert r.headers.get("Content-Security-Policy") == "default-src 'self'"


def test_nonce_template():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(policy="script-src 'self' {nonce}"))
    seen = {}

    @app.get("/")
    async def index(request):
        seen["nonce"] = csp_nonce(request)
        return Response(body=b"x")

    r = TestClient(app).get("/")
    csp = r.headers["Content-Security-Policy"]
    assert "'nonce-" in csp
    assert f"'nonce-{seen['nonce']}'" in csp


def test_per_request_uniqueness():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(policy="script-src {nonce}"))

    @app.get("/")
    async def index(request):
        return Response(body=b"x")

    client = TestClient(app)
    a = client.get("/").headers["Content-Security-Policy"]
    b = client.get("/").headers["Content-Security-Policy"]
    assert a != b


def test_dict_policy_form():
    app = Veloce(openapi_url=None)
    app.add_middleware(
        CSPMiddleware(policy={"default-src": "'self'", "script-src": ["'self'", "'nonce'"]})
    )

    @app.get("/")
    async def index(request):
        return Response(body=b"x")

    csp = TestClient(app).get("/").headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self' 'nonce-" in csp


def test_report_only_independence():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(report_only_policy="default-src 'self'"))

    @app.get("/")
    async def index(request):
        return Response(body=b"x")

    r = TestClient(app).get("/")
    assert r.headers.get("Content-Security-Policy-Report-Only") == "default-src 'self'"
    assert "Content-Security-Policy" not in r.headers or r.headers.get(
        "Content-Security-Policy"
    ) == r.headers.get("Content-Security-Policy-Report-Only")
    # Enforce header must be absent.
    assert not any(k.lower() == "content-security-policy" for k in r.headers)


def test_both_headers():
    app = Veloce(openapi_url=None)
    app.add_middleware(
        CSPMiddleware(policy="default-src 'self'", report_only_policy="img-src 'self'")
    )

    @app.get("/")
    async def index(request):
        return Response(body=b"x")

    r = TestClient(app).get("/")
    assert r.headers.get("Content-Security-Policy") == "default-src 'self'"
    assert r.headers.get("Content-Security-Policy-Report-Only") == "img-src 'self'"


def test_no_clobber():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(policy="default-src 'self'"))

    @app.get("/")
    async def index(request):
        return Response(body=b"x", headers={"Content-Security-Policy": "custom"})

    r = TestClient(app).get("/")
    assert r.headers.get("Content-Security-Policy") == "custom"


def test_no_clobber_lowercase_override():
    """A lowercase route override must suppress the default (RFC 9110 §5.1).

    Header field names are case-insensitive; emitting a second CSP header would
    make browsers intersect the two policies, silently narrowing the route's
    intended policy.
    """
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(policy="default-src 'self'"))

    @app.get("/")
    async def index(request):
        return Response(body=b"x", headers={"content-security-policy": "custom"})

    r = TestClient(app).get("/")
    csp_headers = [
        v.decode("latin-1")
        for k, v in r.raw_headers
        if k.decode("latin-1").lower() == "content-security-policy"
    ]
    assert csp_headers == ["custom"]


def test_report_only_no_clobber_lowercase_override():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(report_only_policy="default-src 'self'"))

    @app.get("/")
    async def index(request):
        return Response(
            body=b"x",
            headers={"content-security-policy-report-only": "custom"},
        )

    r = TestClient(app).get("/")
    csp_headers = [
        v.decode("latin-1")
        for k, v in r.raw_headers
        if k.decode("latin-1").lower() == "content-security-policy-report-only"
    ]
    assert csp_headers == ["custom"]


def test_lazy_materialization_without_handler_read():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(policy="script-src {nonce}"))

    @app.get("/")
    async def index(request):
        return Response(body=b"x")

    r = TestClient(app).get("/")
    assert "'nonce-" in r.headers["Content-Security-Policy"]


def test_a_non_string_policy_is_a_type_error():
    # The empty-configuration refusal is next door in
    # `test_csp_refusal_under_optimisation.py`, which also covers the part that
    # matters about it - that it is a `ValueError` rather than an `assert`, so
    # `python -O` cannot strip it. Asserting it here too said less and said it
    # twice.
    with pytest.raises(TypeError):
        CSPMiddleware(policy=123)  # type: ignore[arg-type]


def test_nonce_disabled_with_placeholder_rejected():
    # A template still referencing a nonce while nonce generation is off would
    # render 'nonce-None' (a real but wrong nonce to browsers). Construction
    # must fail fast instead of emitting the misleading header.
    with pytest.raises(ValueError):
        CSPMiddleware(policy="script-src 'self' {nonce}", nonce=False)
    # Same guard for a directive mapping whose 'nonce' source normalizes to
    # {nonce}, and for a report-only template.
    with pytest.raises(ValueError):
        CSPMiddleware(policy={"script-src": ["'self'", "'nonce'"]}, nonce=False)
    with pytest.raises(ValueError):
        CSPMiddleware(report_only_policy="script-src {nonce}", nonce=False)
    # A nonce-free policy with nonce=False is fine.
    CSPMiddleware(policy="default-src 'self'", nonce=False)


def test_public_import():
    """The names are re-exported from the package root, not only from the
    middleware module. The body used to be the import alone, which this module
    already performs at the top - so it could not have failed here without
    failing collection first."""

    assert veloce.CSPMiddleware is CSPMiddleware
    assert veloce.csp_nonce is csp_nonce
    assert "CSPMiddleware" in veloce.__all__
    assert "csp_nonce" in veloce.__all__
