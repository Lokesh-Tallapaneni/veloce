"""TrustedHost wildcards + HTTPSRedirect scope.scheme + 308 (M6, M7)."""

from __future__ import annotations

import pytest

from veloce import HTTPSRedirectMiddleware, Request, TrustedHostMiddleware, Veloce
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


def test_wildcard_suffixes_dotted_precomputed_with_leading_dot():
    """The wildcard suffix tuple bakes in the leading dot at construction so
    the per-request check is one `endswith` call, not a fresh string concat."""
    mw = TrustedHostMiddleware(allowed_hosts=["*.example.com", "exact.com", "*.OTHER.com"])
    assert mw._wildcard_suffixes_dotted == (".example.com", ".other.com")
    assert not hasattr(mw, "_wildcard_suffixes")


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


async def test_no_redirect_when_scope_scheme_https():
    """If the ASGI server says the transport is already HTTPS, no redirect."""
    # We can't easily inject scope.scheme through TestClient, so verify via
    # direct dispatch of a synthetic scope.

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
    resp = await app.handle_request(req)
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


# ── Exempt paths (ACME default + custom prefixes) ────────────────────


def _make_exempt_app(**mw_kwargs) -> Veloce:
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(HTTPSRedirectMiddleware(**mw_kwargs))

    @app.get("/.well-known/acme-challenge/{token}")
    async def acme(token: str):
        return {"token": token}

    @app.get("/health/{sub}")
    async def health(sub: str):
        return {"ok": True}

    @app.get("/x")
    async def x():
        return {"ok": True}

    return app


def test_acme_challenge_exempt_by_default():
    client = TestClient(_make_exempt_app())
    resp = client.get("/.well-known/acme-challenge/abc", headers={"host": "example.com"})
    assert resp.status_code == 200


def test_non_exempt_path_still_redirected():
    client = TestClient(_make_exempt_app())
    assert client.get("/x", headers={"host": "example.com"}).status_code == 308


def test_acme_exempt_can_be_disabled():
    client = TestClient(_make_exempt_app(exempt_acme_challenge=False))
    resp = client.get("/.well-known/acme-challenge/abc", headers={"host": "example.com"})
    assert resp.status_code == 308


def test_custom_exempt_prefix_match():
    client = TestClient(_make_exempt_app(exempt_paths=("/health/",)))
    assert client.get("/health/live", headers={"host": "example.com"}).status_code == 200
    assert client.get("/x", headers={"host": "example.com"}).status_code == 308


def test_default_exempt_paths_tuple():
    mw = HTTPSRedirectMiddleware()
    assert mw._exempt_paths == ("/.well-known/acme-challenge/",)


def test_trusted_host_middleware_rejects_websocket_from_bad_host():
    app = Veloce(openapi_url=None)
    app.add_middleware(TrustedHostMiddleware(allowed_hosts=["good.example"]))

    @app.websocket("/ws")
    async def handler(ws):
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()

    client = app.test_client()

    with client.websocket_connect("/ws", headers={"host": "good.example"}) as conn:
        assert conn.receive_text() == "hi"

    with (
        pytest.raises(RuntimeError),
        client.websocket_connect("/ws", headers={"host": "evil.example"}),
    ):
        pass
