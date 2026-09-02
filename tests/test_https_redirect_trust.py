"""`HTTPSRedirectMiddleware` respects the hop `ProxyFix` trusted.

`URL.from_request` stands its `X-Forwarded-Proto` fallback down once `ProxyFix`
has run, so a hop refused by trust depth cannot set `request.scheme`. This
middleware was a second, independent reader of the same header and kept its own
fallback, so a single spoofed `X-Forwarded-Proto: https` still suppressed the
upgrade redirect — the one response whose whole job is to get the client off
cleartext. A TLS-stripping attacker on the path needed one header.

The fallback itself is kept: with no `ProxyFix` installed nothing has judged the
header, and honouring it is the documented behaviour for a proxy that does not
set the ASGI scope.
"""

from __future__ import annotations

from veloce import Veloce
from veloce.middleware.proxy_fix import ProxyFix
from veloce.middleware.security import HTTPSRedirectMiddleware
from veloce.testclient import TestClient


def _client(*, proxy_fix: ProxyFix | None = None) -> TestClient:
    app = Veloce(openapi_url=None)
    if proxy_fix is not None:
        app.add_middleware(proxy_fix)
    app.add_middleware(HTTPSRedirectMiddleware())

    @app.get("/thing")
    async def thing():
        return {"ok": True}

    return TestClient(app)


# ── The bypass ───────────────────────────────────────────────────────


def test_a_refused_hop_does_not_suppress_the_redirect():
    """The defect: one spoofed header and the client stays on cleartext."""
    with _client(proxy_fix=ProxyFix(x_proto=2)) as client:
        response = client.get(
            "/thing", headers={"x-forwarded-proto": "https"}, follow_redirects=False
        )
    assert response.status_code == 308, "a hop ProxyFix refused suppressed the upgrade"
    assert response.headers["location"].startswith("https://")


def test_a_refused_hop_is_consistent_with_the_request_scheme():
    """Both readers of the header must reach the same verdict."""
    app = Veloce(openapi_url=None)
    app.add_middleware(ProxyFix(x_proto=2))

    @app.get("/scheme")
    async def scheme(request):  # noqa: ANN001, ANN202
        return {"scheme": request.scheme}

    with TestClient(app) as client:
        body = client.get("/scheme", headers={"x-forwarded-proto": "https"}).json()
    assert body["scheme"] == "http"


# ── What must keep working ───────────────────────────────────────────


def test_a_trusted_hop_still_suppresses_the_redirect():
    """ProxyFix writes the trusted scheme into the scope; that must be honoured."""
    with _client(proxy_fix=ProxyFix(x_proto=1)) as client:
        response = client.get(
            "/thing", headers={"x-forwarded-proto": "https"}, follow_redirects=False
        )
    assert response.status_code == 200


def test_the_header_is_still_honoured_with_no_proxyfix():
    """Unchanged behaviour for a proxy that does not set the ASGI scope."""
    with _client() as client:
        response = client.get(
            "/thing", headers={"x-forwarded-proto": "https"}, follow_redirects=False
        )
    assert response.status_code == 200


def test_plain_http_still_redirects():
    with _client() as client:
        response = client.get("/thing", follow_redirects=False)
    assert response.status_code == 308


def test_a_trusted_forwarded_header_also_suppresses_the_redirect():
    """RFC 7239 spelling, trusted at the configured depth."""
    with _client(proxy_fix=ProxyFix(x_proto=1, trust_forwarded=True)) as client:
        response = client.get(
            "/thing", headers={"forwarded": "proto=https"}, follow_redirects=False
        )
    assert response.status_code == 200
