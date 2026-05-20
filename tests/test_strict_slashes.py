"""Per-route `strict_slashes=False` override."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce


def _req(path: str) -> Request:
    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


# ── Default (strict) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_redirects_when_slash_mismatches():
    """Default behaviour — `/x/` redirects to `/x` (or vice versa)."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return {}

    resp = await app.handle_request(_req("/x/"))
    # Without strict_slashes=False, a slashed request gets a redirect.
    assert resp.status_code in (307, 308)


# ── strict_slashes=False ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_strict_slashes_false_matches_both_forms():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x", strict_slashes=False)
    async def x():
        return {"ok": True}

    # Both forms reach the handler — no redirect.
    resp1 = await app.handle_request(_req("/x"))
    resp2 = await app.handle_request(_req("/x/"))
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    import orjson

    assert orjson.loads(resp1.body) == {"ok": True}
    assert orjson.loads(resp2.body) == {"ok": True}


@pytest.mark.asyncio
async def test_strict_slashes_false_with_trailing_form_too():
    """Registering with the slashed form + strict_slashes=False also
    accepts the unslashed form."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/items/", strict_slashes=False)
    async def items():
        return {"items": []}

    resp1 = await app.handle_request(_req("/items"))
    resp2 = await app.handle_request(_req("/items/"))
    assert resp1.status_code == 200
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_strict_slashes_only_affects_decorated_route():
    """Other routes still follow the global redirect_slashes policy."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/relaxed", strict_slashes=False)
    async def relaxed():
        return {}

    @app.get("/strict")
    async def strict():
        return {}

    # /relaxed/ matches without redirect.
    r1 = await app.handle_request(_req("/relaxed/"))
    assert r1.status_code == 200
    # /strict/ redirects.
    r2 = await app.handle_request(_req("/strict/"))
    assert r2.status_code in (307, 308)
