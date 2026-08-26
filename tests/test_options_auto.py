"""Auto-OPTIONS with Allow header (RFC 9110 §9.3.7)."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce


def _req(method: str, path: str = "/x") -> Request:
    return make_request(method=method, path=path, query_string="", headers={}, body=b"")


@pytest.mark.asyncio
async def test_options_returns_200_with_allow_header():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return {}

    @app.post("/x")
    async def xp():
        return {}

    resp = await app.handle_request(_req("OPTIONS", "/x"))
    assert resp.status_code == 200
    assert resp.body == b""
    allow = resp.headers.get("Allow") or resp.headers.get("allow")
    methods = {m.strip() for m in allow.split(",")}
    assert "GET" in methods
    assert "POST" in methods
    assert "OPTIONS" in methods
    # HEAD added because GET is present.
    assert "HEAD" in methods


@pytest.mark.asyncio
async def test_options_on_path_without_get_omits_head():
    app = Veloce(debug=True, openapi_url=None)

    @app.post("/x")
    async def xp():
        return {}

    resp = await app.handle_request(_req("OPTIONS", "/x"))
    assert resp.status_code == 200
    allow = resp.headers.get("Allow") or resp.headers.get("allow")
    methods = {m.strip() for m in allow.split(",")}
    assert "POST" in methods
    assert "OPTIONS" in methods
    assert "HEAD" not in methods
    assert "GET" not in methods


@pytest.mark.asyncio
async def test_explicit_options_handler_wins():
    """When the user registers OPTIONS explicitly, that handler runs."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return {}

    @app.route("/x", methods=["OPTIONS"])
    async def x_options():
        return {"custom": True}

    resp = await app.handle_request(_req("OPTIONS", "/x"))
    assert resp.status_code == 200
    # Custom handler returns JSON, not empty body.
    assert resp.body == b'{"custom":true}'


@pytest.mark.asyncio
async def test_options_unknown_path_still_404():
    app = Veloce(debug=True, openapi_url=None)
    resp = await app.handle_request(_req("OPTIONS", "/missing"))
    assert resp.status_code == 404
