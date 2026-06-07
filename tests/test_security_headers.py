"""SecurityHeadersMiddleware — hardening response headers (S4)."""

from __future__ import annotations

from veloce import Response, SecurityHeadersMiddleware, Veloce
from veloce.testclient import TestClient


def _app(**mw_kwargs: object) -> Veloce:
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(SecurityHeadersMiddleware(**mw_kwargs))

    @app.get("/x")
    async def x():
        return {"ok": True}

    return app


def test_default_headers_present():
    resp = TestClient(_app()).get("/x")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_handler_lowercase_header_override_is_preserved():
    # `Response.headers` is case-sensitive, but a handler-set lowercase
    # `x-frame-options` must still count as an override of the default, not be
    # silently clobbered with `DENY`.
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(SecurityHeadersMiddleware())

    @app.get("/y")
    async def y():
        return Response(body=b"{}", headers={"x-frame-options": "SAMEORIGIN"})

    resp = TestClient(app).get("/y")
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"


def test_hsts_off_by_default():
    resp = TestClient(_app()).get("/x")
    assert "strict-transport-security" not in resp.headers


def test_hsts_emitted_when_configured():
    resp = TestClient(_app(hsts_max_age=600)).get("/x")
    hsts = resp.headers["strict-transport-security"]
    assert "max-age=600" in hsts
    # `includeSubDomains` is opt-in (off by default since a casual
    # `hsts_max_age=...` on a multi-subdomain host shouldn't silently
    # pin every subdomain). The flag flips back on when explicitly set.
    assert "includeSubDomains" not in hsts


def test_hsts_include_subdomains_when_opted_in():
    resp = TestClient(_app(hsts_max_age=600, hsts_include_subdomains=True)).get("/x")
    assert "includeSubDomains" in resp.headers["strict-transport-security"]


def test_hsts_preload_flag():
    resp = TestClient(_app(hsts_max_age=600, hsts_preload=True)).get("/x")
    assert "preload" in resp.headers["strict-transport-security"]


def test_csp_emitted_when_configured():
    resp = TestClient(_app(content_security_policy="default-src 'self'")).get("/x")
    assert resp.headers["content-security-policy"] == "default-src 'self'"


def test_handler_value_not_overwritten():
    """A header the handler set itself wins over the middleware default."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(SecurityHeadersMiddleware())

    @app.get("/x")
    async def x():
        resp = Response(body=b"ok")
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        return resp

    resp = TestClient(app).get("/x")
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"


def test_frame_options_can_be_disabled():
    resp = TestClient(_app(frame_options=None)).get("/x")
    assert "x-frame-options" not in resp.headers
    # The other defaults still apply.
    assert resp.headers["x-content-type-options"] == "nosniff"
