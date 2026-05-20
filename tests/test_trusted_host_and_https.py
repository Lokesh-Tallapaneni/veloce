"""TrustedHost wildcards + HTTPSRedirect scope.scheme + 308 (M6, M7)."""

from __future__ import annotations

from veloce import HTTPSRedirectMiddleware, TrustedHostMiddleware, Veloce
from veloce.testclient import TestClient

# ── M6: TrustedHostMiddleware wildcards ────────────────────────────────


def _make_th_app(allowed: list[str]) -> Veloce:
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware(allowed_hosts=allowed))

    @app.get("/x")
    async def x():
        return {"ok": True}

    return app


def test_exact_host_match_allowed():
    client = TestClient(_make_th_app(["example.com"]))
    resp = client.get("/x", headers={"host": "example.com"})
    assert resp.status_code == 200


def test_exact_host_mismatch_rejected():
    client = TestClient(_make_th_app(["example.com"]))
    resp = client.get("/x", headers={"host": "evil.example"})
    assert resp.status_code == 400


def test_port_stripped_before_matching():
    """`Host: example.com:8080` must still match `example.com` (RFC 9110 §7.2)."""
    client = TestClient(_make_th_app(["example.com"]))
    resp = client.get("/x", headers={"host": "example.com:8080"})
    assert resp.status_code == 200


def test_subdomain_wildcard_matches_subdomain():
    client = TestClient(_make_th_app(["*.example.com"]))
    resp = client.get("/x", headers={"host": "api.example.com"})
    assert resp.status_code == 200


def test_subdomain_wildcard_matches_nested_subdomain():
    """`*.example.com` matches `a.b.example.com` — common nginx convention."""
    client = TestClient(_make_th_app(["*.example.com"]))
    resp = client.get("/x", headers={"host": "a.b.example.com"})
    assert resp.status_code == 200


def test_subdomain_wildcard_does_not_match_apex():
    """`*.example.com` does NOT match the bare `example.com`."""
    client = TestClient(_make_th_app(["*.example.com"]))
    resp = client.get("/x", headers={"host": "example.com"})
    assert resp.status_code == 400


def test_subdomain_wildcard_rejects_unrelated_host():
    client = TestClient(_make_th_app(["*.example.com"]))
    resp = client.get("/x", headers={"host": "example.org"})
    assert resp.status_code == 400


def test_wildcard_star_allows_anything():
    client = TestClient(_make_th_app(["*"]))
    resp = client.get("/x", headers={"host": "literally.anything.example"})
    assert resp.status_code == 200


def test_multiple_wildcards_and_exacts_coexist():
    client = TestClient(_make_th_app(["example.com", "*.example.com", "api.other.com"]))
    assert client.get("/x", headers={"host": "example.com"}).status_code == 200
    assert client.get("/x", headers={"host": "a.example.com"}).status_code == 200
    assert client.get("/x", headers={"host": "api.other.com"}).status_code == 200
    assert client.get("/x", headers={"host": "other.com"}).status_code == 400


def test_match_is_case_insensitive():
    client = TestClient(_make_th_app(["example.com"]))
    resp = client.get("/x", headers={"host": "EXAMPLE.COM"})
    assert resp.status_code == 200


# ── M7: HTTPSRedirectMiddleware uses scope.scheme + 308 ────────────────


def _make_https_app() -> Veloce:
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(HTTPSRedirectMiddleware())

    @app.get("/x")
    async def x():
        return {"ok": True}

    @app.post("/x")
    async def x_post():
        return {"ok": True}

    return app


def test_redirect_uses_308_to_preserve_method():
    """A POST to http://… must redirect with 308, not 301 (which would
    silently convert to GET on most clients)."""
    client = TestClient(_make_https_app())
    resp = client.post("/x", headers={"host": "example.com"})
    assert resp.status_code == 308
    assert resp.headers.get("location", "").startswith("https://example.com")


def test_redirect_includes_query_string():
    client = TestClient(_make_https_app())
    resp = client.get("/x?a=1&b=2", headers={"host": "example.com"})
    assert resp.status_code == 308
    loc = resp.headers.get("location", "")
    assert "?a=1&b=2" in loc


def test_no_redirect_when_scope_scheme_https():
    """If the ASGI server says the transport is already HTTPS, no redirect."""
    # We can't easily inject scope.scheme through TestClient, so verify via
    # direct dispatch of a synthetic scope.
    import asyncio

    from veloce import Request

    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(HTTPSRedirectMiddleware())

    @app.get("/x")
    async def x():
        return {"ok": True}

    req = Request(
        method="GET",
        path="/x",
        query_string="",
        headers={"host": "example.com"},
        body=b"",
        scope={"type": "http", "scheme": "https"},
    )
    resp = asyncio.new_event_loop().run_until_complete(app.handle_request(req))
    assert resp.status_code == 200


def test_no_redirect_when_x_forwarded_proto_https():
    """Behind a TLS-terminating proxy that sets X-Forwarded-Proto."""
    client = TestClient(_make_https_app())
    resp = client.get("/x", headers={"host": "example.com", "x-forwarded-proto": "https"})
    assert resp.status_code == 200


def test_redirects_when_neither_scope_nor_header_says_https():
    client = TestClient(_make_https_app())
    resp = client.get("/x", headers={"host": "example.com"})
    assert resp.status_code == 308
