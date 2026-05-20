"""PEP 593 `Annotated[T, Depends(...)]` support (D11).

NOTE: this test file deliberately does NOT use
`from __future__ import annotations`. PEP 593 metadata that carries
runtime objects (a `Depends()` instance bound to a local function) needs
eager annotation evaluation — string annotations would force callers to
keep markers at module scope. This is the documented usage
pattern.
"""

from typing import Annotated

import pytest

from veloce import Depends, Header, Query, Request, Security, Veloce


def _req(path: str = "/x", query: str = "") -> Request:
    return Request(method="GET", path=path, query_string=query, headers={}, body=b"")


# ── Annotated + Depends ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_annotated_depends_resolves():
    """`db: Annotated[Session, Depends(get_db)]` works without a default value."""
    app = Veloce(debug=True, openapi_url=None)

    def get_db() -> dict:
        return {"connected": True}

    @app.get("/x")
    async def x(db: Annotated[dict, Depends(get_db)]):
        return db

    resp = await app.handle_request(_req())
    assert resp.status_code == 200
    assert b'"connected":true' in resp.body


@pytest.mark.asyncio
async def test_annotated_depends_with_sub_depends():
    """Annotated form composes — sub-dependencies still resolve."""
    app = Veloce(debug=True, openapi_url=None)

    def get_id() -> int:
        return 42

    def get_user(id: Annotated[int, Depends(get_id)]) -> dict:
        return {"id": id, "name": "alice"}

    @app.get("/me")
    async def me(user: Annotated[dict, Depends(get_user)]):
        return user

    resp = await app.handle_request(_req("/me"))
    assert b'"id":42' in resp.body
    assert b'"name":"alice"' in resp.body


# ── Annotated + Security ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_annotated_security_passes_scopes():
    """Annotated form preserves Security.scopes for the scope stack."""
    from veloce import SecurityScopes

    app = Veloce(debug=True, openapi_url=None)
    captured: dict = {}

    def auth(scopes: SecurityScopes) -> str:
        captured["scopes"] = list(scopes.scopes)
        return "user"

    @app.get("/x")
    async def x(user: Annotated[str, Security(auth, scopes=["admin"])]):
        return {"user": user}

    await app.handle_request(_req())
    assert captured["scopes"] == ["admin"]


# ── Annotated + Query / Header / Path markers ─────────────────────────


@pytest.mark.asyncio
async def test_annotated_query_marker():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x(q: Annotated[str, Query(default="default")]):
        return {"q": q}

    resp = await app.handle_request(_req(query="q=hello"))
    assert b'"hello"' in resp.body


@pytest.mark.asyncio
async def test_annotated_query_uses_default_when_missing():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x(q: Annotated[str, Query(default="fallback")]):
        return {"q": q}

    resp = await app.handle_request(_req())
    assert b'"fallback"' in resp.body


@pytest.mark.asyncio
async def test_annotated_header_marker_with_alias():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x(token: Annotated[str, Header(alias="X-Token", default="")]):
        return {"token": token}

    req = Request(
        method="GET",
        path="/x",
        query_string="",
        headers={"X-Token": "abc"},
        body=b"",
    )
    resp = await app.handle_request(req)
    assert b'"abc"' in resp.body


# ── Default-value form still wins when both are set ───────────────────


@pytest.mark.asyncio
async def test_default_value_wins_over_annotated_when_both_present():
    """When both `Annotated[T, Depends(a)]` AND `= Depends(b)` are set,
    the default-value form takes precedence (matches user intent on the
    visible call site)."""
    app = Veloce(debug=True, openapi_url=None)

    def in_annotation() -> str:
        return "annotated"

    def in_default() -> str:
        return "default"

    @app.get("/x")
    async def x(
        v: Annotated[str, Depends(in_annotation)] = Depends(in_default),
    ):
        return {"v": v}

    resp = await app.handle_request(_req())
    assert b'"default"' in resp.body


# ── Mixed with regular annotations ────────────────────────────────────


@pytest.mark.asyncio
async def test_annotated_does_not_break_plain_query_params():
    """Non-Depends Annotated metadata is ignored; the inner type is used
    for query coercion."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x(count: Annotated[int, "some unrelated annotation"] = 0):
        return {"count": count, "type": type(count).__name__}

    resp = await app.handle_request(_req(query="count=7"))
    body = resp.body
    assert b'"count":7' in body
    assert b'"type":"int"' in body
