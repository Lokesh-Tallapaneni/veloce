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


def test_hsts_off_by_default():
    resp = TestClient(_app()).get("/x")
    assert "strict-transport-security" not in resp.headers


def test_hsts_emitted_when_configured():
    resp = TestClient(_app(hsts_max_age=600)).get("/x")
    hsts = resp.headers["strict-transport-security"]
    assert "max-age=600" in hsts
    assert "includeSubDomains" in hsts


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
