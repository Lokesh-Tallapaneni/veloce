"""CORS spec-compliance tests (M4)."""

from __future__ import annotations

import pytest

from veloce import CORSMiddleware, Veloce
from veloce.testclient import TestClient

# ── Construction validation ────────────────────────────────────────────


def test_wildcard_with_credentials_rejected_at_construction():
    """`Access-Control-Allow-Origin: *` + credentials is forbidden by the
    Fetch CORS spec — construction must fail loudly."""
    with pytest.raises(ValueError):
        CORSMiddleware(allow_origins=["*"], allow_credentials=True)


def test_wildcard_headers_with_credentials_rejected():
    with pytest.raises(ValueError):
        CORSMiddleware(
            allow_origins=["http://x.com"],
            allow_headers=["*"],
            allow_credentials=True,
        )


# ── Allow-origin resolution ────────────────────────────────────────────


def _make_app(**kwargs) -> Veloce:
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CORSMiddleware(**kwargs))

    @app.get("/x")
    async def x():
        return {"ok": True}

    return app


def test_wildcard_returns_star_when_credentials_off():
    client = TestClient(_make_app(allow_origins=["*"]))
    resp = client.get("/x", headers={"origin": "http://anyone.example"})
    assert resp.headers.get("access-control-allow-origin") == "*"


def test_credentials_echoes_exact_origin_never_star():
    # `allow_headers=["*"]` is forbidden with credentials, so be explicit.
    client = TestClient(
        _make_app(
            allow_origins=["http://allowed.example"],
            allow_headers=["Content-Type", "Authorization"],
            allow_credentials=True,
        )
    )
    resp = client.get("/x", headers={"origin": "http://allowed.example"})
    assert resp.headers.get("access-control-allow-origin") == "http://allowed.example"
    assert resp.headers.get("access-control-allow-credentials") == "true"


def test_disallowed_origin_gets_no_allow_origin_header():
    client = TestClient(_make_app(allow_origins=["http://allowed.example"]))
    resp = client.get("/x", headers={"origin": "http://evil.example"})
    assert resp.headers.get("access-control-allow-origin") is None


def test_regex_matches_subdomain_pattern():
    # No `*` in allow_origins — only the regex gates membership.
    client = TestClient(_make_app(allow_origins=[], allow_origin_regex=r"https://.*\.example\.com"))
    resp = client.get("/x", headers={"origin": "https://api.example.com"})
    assert resp.headers.get("access-control-allow-origin") == "https://api.example.com"

    resp2 = client.get("/x", headers={"origin": "https://example.com"})
    # Not a subdomain match — must be denied.
    assert resp2.headers.get("access-control-allow-origin") is None


# ── Vary: Origin ───────────────────────────────────────────────────────


def test_vary_origin_emitted_when_origin_echoed():
    """Whenever ACAO depends on the request origin, Vary: Origin is required
    to prevent cache poisoning across origins."""
    client = TestClient(_make_app(allow_origins=["http://a.example", "http://b.example"]))
    resp = client.get("/x", headers={"origin": "http://a.example"})
    assert "Origin" in resp.headers.get("vary", "")


def test_vary_origin_not_added_for_pure_wildcard():
    """`*` without credentials is origin-agnostic; Vary: Origin is unneeded."""
    client = TestClient(_make_app(allow_origins=["*"]))
    resp = client.get("/x", headers={"origin": "http://anywhere.example"})
    # `*` mode: no need for Vary on Origin.
    vary = resp.headers.get("vary", "")
    assert "Origin" not in vary


def test_vary_appended_not_replaced():
    """A pre-existing Vary value must be preserved when we add Origin."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CORSMiddleware(allow_origins=["http://a.example"]))

    @app.get("/x")
    async def x():
        from veloce.http.response import Response

        return Response(body=b"ok", content_type="text/plain", headers={"Vary": "Accept"})

    resp = TestClient(app).get("/x", headers={"origin": "http://a.example"})
    vary = resp.headers.get("vary", "")
    assert "Accept" in vary
    assert "Origin" in vary


# ── Preflight (OPTIONS) ────────────────────────────────────────────────


def test_preflight_with_acrm_echoes_method_and_filtered_headers():
    client = TestClient(
        _make_app(
            allow_origins=["http://a.example"],
            allow_headers=["X-Custom", "Authorization"],
        )
    )
    resp = client.options(
        "/x",
        headers={
            "origin": "http://a.example",
            "access-control-request-method": "POST",
            "access-control-request-headers": "X-Custom, X-Disallowed",
        },
    )
    assert resp.status_code == 204
    # Only the allowed header from the requested set echoes back.
    allowed = resp.headers.get("access-control-allow-headers", "")
    assert "X-Custom" in allowed
    assert "X-Disallowed" not in allowed


def test_preflight_with_wildcard_headers_echoes_requested():
    """allow_headers=['*'] + a specific request-headers list → echo back
    exactly what was requested."""
    client = TestClient(_make_app(allow_origins=["http://a.example"], allow_headers=["*"]))
    resp = client.options(
        "/x",
        headers={
            "origin": "http://a.example",
            "access-control-request-method": "POST",
            "access-control-request-headers": "X-One, X-Two",
        },
    )
    assert resp.headers.get("access-control-allow-headers") == "X-One, X-Two"


def test_preflight_from_disallowed_origin_is_rejected():
    """A preflight from a disallowed origin gets a diagnostic 400 and no
    Access-Control-Allow-* headers."""
    client = TestClient(_make_app(allow_origins=["http://a.example"]))
    resp = client.options(
        "/x",
        headers={
            "origin": "http://evil.example",
            "access-control-request-method": "POST",
        },
    )
    assert resp.status_code == 400
    assert resp.headers.get("access-control-allow-origin") is None


def test_options_without_origin_passes_through():
    """An OPTIONS request with no Origin isn't CORS — the middleware should
    not synthesise a preflight response."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CORSMiddleware(allow_origins=["*"]))

    @app.options("/x")
    async def x():
        return {"reached_handler": True}

    resp = TestClient(app).options("/x")
    assert resp.json() == {"reached_handler": True}


# ── Expose-headers + max-age ───────────────────────────────────────────


def test_expose_headers_emitted():
    client = TestClient(_make_app(allow_origins=["*"], expose_headers=["X-Total", "X-Rate"]))
    resp = client.get("/x", headers={"origin": "http://any.example"})
    expose = resp.headers.get("access-control-expose-headers", "")
    assert "X-Total" in expose and "X-Rate" in expose


def test_preflight_emits_max_age():
    client = TestClient(_make_app(allow_origins=["*"], max_age=86400))
    resp = client.options(
        "/x",
        headers={"origin": "http://any.example", "access-control-request-method": "GET"},
    )
    assert resp.headers.get("access-control-max-age") == "86400"
