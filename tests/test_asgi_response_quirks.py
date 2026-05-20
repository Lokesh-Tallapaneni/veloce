"""ASGI response correctness: multi-Set-Cookie + 204/304/205 strip body (Q44, Q47)."""

from __future__ import annotations

import pytest

from veloce import Response, Veloce
from veloce.testclient import TestClient

# ── Q44: Multi-Set-Cookie ASGI emission ────────────────────────────────


def test_multiple_set_cookies_emit_as_separate_asgi_headers():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/multi")
    async def multi():
        r = Response(body=b"ok", content_type="text/plain")
        r.set_cookie("a", "1")
        r.set_cookie("b", "2")
        r.set_cookie("c", "3")
        return r

    client = TestClient(app)
    resp = client.get("/multi")

    # Each Set-Cookie should be its own entry in the raw header list.
    set_cookie_lines = [v.decode() for k, v in resp.raw_headers if k == b"set-cookie"]
    assert len(set_cookie_lines) == 3
    assert any("a=1" in line for line in set_cookie_lines)
    assert any("b=2" in line for line in set_cookie_lines)
    assert any("c=3" in line for line in set_cookie_lines)
    # And the cookies dict picks up all three.
    assert resp.cookies == {"a": "1", "b": "2", "c": "3"}


def test_single_set_cookie_still_emits_one_header():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/one")
    async def one():
        r = Response(body=b"ok", content_type="text/plain")
        r.set_cookie("session", "abc")
        return r

    client = TestClient(app)
    resp = client.get("/one")
    cookies = [v for k, v in resp.raw_headers if k == b"set-cookie"]
    assert len(cookies) == 1
    assert resp.cookies == {"session": "abc"}


# ── Q47: 204 / 304 / 205 strip body ───────────────────────────────────


@pytest.mark.parametrize("code", [204, 304, 205])
def test_no_body_status_codes_strip_body(code):
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x", status_code=code)
    async def h():
        return {"this": "should be stripped"}

    client = TestClient(app)
    resp = client.get("/x")
    assert resp.status_code == code
    assert resp.body == b""
    # And content-length MUST reflect the empty body (HTTP intermediaries
    # treat content-length: 23 with empty body as malformed).
    cl = next((v.decode() for k, v in resp.raw_headers if k == b"content-length"), None)
    assert cl == "0"


def test_200_response_body_unchanged():
    """Sanity: only the no-body codes get stripped; 200 keeps its payload."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def h():
        return {"k": "v"}

    client = TestClient(app)
    resp = client.get("/x")
    assert resp.status_code == 200
    assert resp.body == b'{"k":"v"}'


def test_explicit_response_with_204_status_strips_body():
    """A handler that returns a Response(204, body=...) should also have its
    body stripped — the spec applies regardless of how the status was set."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def h():
        return Response(status_code=204, body=b"nope", content_type="text/plain")

    client = TestClient(app)
    resp = client.get("/x")
    assert resp.status_code == 204
    assert resp.body == b""
