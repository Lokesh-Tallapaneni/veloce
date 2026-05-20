"""Subdomain routing."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce


def _req(path: str, host: str) -> Request:
    return Request(
        method="GET",
        path=path,
        query_string="",
        headers={"host": host},
        body=b"",
    )


@pytest.mark.asyncio
async def test_subdomain_match_by_leftmost_label_without_server_name():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x", subdomain="api")
    async def api_only():
        return {"hit": "api"}

    # No SERVER_NAME → leftmost-label match.
    r = await app.handle_request(_req("/x", host="api.example.com"))
    assert r.status_code == 200
    import orjson

    assert orjson.loads(r.body) == {"hit": "api"}


@pytest.mark.asyncio
async def test_subdomain_mismatch_returns_404():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x", subdomain="api")
    async def api_only():
        return {}

    r = await app.handle_request(_req("/x", host="other.example.com"))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_subdomain_match_with_server_name_configured():
    app = Veloce(debug=True, openapi_url=None)
    app.config["SERVER_NAME"] = "example.com"

    @app.get("/x", subdomain="api")
    async def api_only():
        return {"v": 1}

    r = await app.handle_request(_req("/x", host="api.example.com"))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_subdomain_wildcard_matches_any_subdomain():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x", subdomain="*")
    async def any_sub():
        return {"ok": True}

    r1 = await app.handle_request(_req("/x", host="alpha.example.com"))
    r2 = await app.handle_request(_req("/x", host="beta.example.com"))
    assert r1.status_code == 200
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_subdomain_apex_does_not_match_wildcard():
    app = Veloce(debug=True, openapi_url=None)
    app.config["SERVER_NAME"] = "example.com"

    @app.get("/x", subdomain="*")
    async def sub_only():
        return {}

    # Apex (no subdomain) does not match `*`.
    r = await app.handle_request(_req("/x", host="example.com"))
    assert r.status_code == 404


def test_request_subdomain_property_with_server_name():
    app = Veloce(openapi_url=None)
    app.config["SERVER_NAME"] = "example.com"
    req = _req("/x", "api.example.com")
    req.app = app
    assert req.subdomain == "api"
    req2 = _req("/x", "example.com")
    req2.app = app
    assert req2.subdomain == ""


def test_request_subdomain_property_without_server_name():
    """Without SERVER_NAME, leftmost-label heuristic; apex returns ''."""
    app = Veloce(openapi_url=None)
    req_sub = _req("/x", "api.example.com")
    req_sub.app = app
    assert req_sub.subdomain == "api"
    req_apex = _req("/x", "localhost")
    req_apex.app = app
    assert req_apex.subdomain == ""
