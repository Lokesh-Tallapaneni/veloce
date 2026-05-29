"""End-to-end HTTP safety tests driven through TestClient.

Each test exercises a fix that lives in the http/* subpackage by
issuing a real request through the in-memory ASGI test client, so the
fix is verified on the actual wire output, not just at the unit-call
boundary.
"""

from __future__ import annotations

from urllib.parse import urlencode

import pytest

from veloce import JSONResponse, Request, Veloce
from veloce.http.cookies import dump_cookie
from veloce.http.response import Response
from veloce.testclient import TestClient


def _make_app() -> Veloce:
    return Veloce(debug=False, openapi_url=None)


# ── Cookies ─────────────────────────────────────────────────────────


def test_cookie_crlf_in_value_does_not_inject_header():
    app = _make_app()

    @app.get("/cookie")
    async def cookie_handler():
        resp = Response(body=b"ok")
        resp.set_cookie("a", "v\r\nInjected: attack")
        return resp

    client = TestClient(app)
    resp = client.get("/cookie")

    assert resp.status_code == 500
    assert "Injected" not in resp.headers
    assert "injected" not in {k.lower() for k, _ in resp.raw_headers}
    for _, raw_value in resp.raw_headers:
        assert b"Injected" not in raw_value
        assert b"attack" not in raw_value


def test_dump_cookie_rejects_crlf_in_samesite():
    with pytest.raises(ValueError):
        dump_cookie("a", "v", samesite="Strict\nInjected")


# ── Response.status setter ──────────────────────────────────────────


def test_response_status_empty_does_not_leak_indexerror():
    app = _make_app()

    @app.get("/status")
    async def status_handler():
        resp = Response(body=b"ok")
        resp.status = ""
        return resp

    client = TestClient(app)
    resp = client.get("/status")
    assert resp.status_code == 500


# ── WWW-Authenticate realm CRLF ─────────────────────────────────────


def test_basic_auth_challenge_realm_crlf_does_not_inject():
    app = _make_app()

    @app.get("/auth")
    async def auth_handler():
        resp = Response(body=b"ok", status_code=401)
        resp.set_basic_auth_challenge(realm="x\r\nInjected: y")
        return resp

    client = TestClient(app)
    resp = client.get("/auth")

    assert resp.status_code == 500
    assert "Injected" not in resp.headers
    for _, raw_value in resp.raw_headers:
        assert b"Injected" not in raw_value


# ── JSONResponse.from_bytes ─────────────────────────────────────────


def test_json_from_bytes_round_trip():
    app = _make_app()

    @app.get("/json")
    async def json_handler():
        return JSONResponse.from_bytes(b'{"a":1}')

    client = TestClient(app)
    resp = client.get("/json")

    assert resp.status_code == 200
    assert resp.body == b'{"a":1}'
    assert resp.content_type == "application/json"


def test_json_from_bytes_caller_content_type_wins():
    app = _make_app()

    @app.get("/problem")
    async def problem_handler():
        return JSONResponse.from_bytes(b"{}", headers={"Content-Type": "application/problem+json"})

    client = TestClient(app)
    resp = client.get("/problem")

    assert resp.status_code == 200
    assert resp.body == b"{}"
    assert resp.content_type == "application/problem+json"


def test_json_from_bytes_rejects_str():
    with pytest.raises(TypeError):
        JSONResponse.from_bytes("not bytes")  # type: ignore[arg-type]


# ── AcceptHeader wildcard for non-MIME headers ──────────────────────


def test_accept_language_bare_wildcard_matches_any_tag():
    app = _make_app()
    seen: dict[str, float] = {}

    @app.get("/lang")
    async def lang_handler(request: Request):
        seen["q"] = request.accept_languages.quality("en-US")
        return {"q": seen["q"]}

    client = TestClient(app)
    resp = client.get("/lang", headers={"Accept-Language": "*"})

    assert resp.status_code == 200
    assert seen["q"] == 1.0
    assert resp.json() == {"q": 1.0}


# ── Query DoS limit ─────────────────────────────────────────────────


def test_query_string_over_limit_returns_414():
    app = _make_app()

    @app.get("/q")
    async def q_handler(request: Request):
        # Touch query_params so the parser runs.
        _ = dict(request.query_params)
        return {"count": len(request.query_params)}

    qs = urlencode([(f"k{i}", "v") for i in range(1001)])
    client = TestClient(app)
    resp = client.get(f"/q?{qs}")
    assert resp.status_code == 414


def test_query_string_exactly_at_limit_succeeds():
    app = _make_app()

    @app.get("/q")
    async def q_handler(request: Request):
        return {"count": len(request.query_params)}

    qs = urlencode([(f"k{i}", "v") for i in range(1000)])
    client = TestClient(app)
    resp = client.get(f"/q?{qs}")
    assert resp.status_code == 200
    assert resp.json() == {"count": 1000}
