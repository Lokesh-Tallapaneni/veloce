"""SecurityScopes injection tests (D6)."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import (
    Depends,
    HTTPBearer,
    OAuth2PasswordBearer,
    Request,
    Security,
    SecurityScopes,
    Veloce,
)
from veloce.testclient import TestClient


def _req(path: str = "/") -> Request:
    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


# ── SecurityScopes class shape ─────────────────────────────────────────


def test_security_scopes_holds_list_and_string():
    s = SecurityScopes(["read", "write"])
    assert s.scopes == ["read", "write"]
    assert s.scope_str == "read write"


def test_security_scopes_empty():
    s = SecurityScopes()
    assert s.scopes == []
    assert s.scope_str == ""


def test_security_scopes_repr():
    assert "SecurityScopes" in repr(SecurityScopes(["a"]))


# ── End-to-end: scopes flow from Security() to the auth callable ─────


@pytest.mark.asyncio
async def test_scopes_reach_sub_dependency():
    app = Veloce(debug=True, openapi_url=None)
    seen: dict = {}

    def auth(security_scopes: SecurityScopes) -> dict:
        seen["scopes"] = list(security_scopes.scopes)
        seen["scope_str"] = security_scopes.scope_str
        return {"ok": True}

    @app.get("/me")
    async def me(user=Security(auth, scopes=["read:profile"])):
        return user

    await app.handle_request(_req("/me"))
    assert seen["scopes"] == ["read:profile"]
    assert seen["scope_str"] == "read:profile"


@pytest.mark.asyncio
async def test_scopes_accumulate_across_nested_security():
    """`Security(inner, scopes=["a"])` then `Security(outer, scopes=["b"])`
    chained must give the innermost auth callable the union of both."""
    app = Veloce(debug=True, openapi_url=None)
    seen: dict = {}

    def base(security_scopes: SecurityScopes):
        seen["base"] = list(security_scopes.scopes)
        return "ok"

    def inner(_=Security(base, scopes=["inner"])):
        return _

    @app.get("/x")
    async def x(_=Security(inner, scopes=["outer"])):
        return {"ok": True}

    await app.handle_request(_req("/x"))
    # Outer scopes pushed first; inner pushed on top; `base` sees union.
    assert seen["base"] == ["outer", "inner"]


@pytest.mark.asyncio
async def test_scopes_do_not_leak_between_requests():
    """Two requests in sequence must not share the resolver's scope stack."""
    app = Veloce(debug=True, openapi_url=None)
    captured: list = []

    def auth(security_scopes: SecurityScopes):
        captured.append(list(security_scopes.scopes))
        return None

    @app.get("/a")
    async def a(_=Security(auth, scopes=["A"])):
        return {}

    @app.get("/b")
    async def b(_=Security(auth, scopes=["B"])):
        return {}

    await app.handle_request(_req("/a"))
    await app.handle_request(_req("/b"))
    assert captured == [["A"], ["B"]]


@pytest.mark.asyncio
async def test_plain_depends_does_not_push_scopes():
    """A non-Security dependency wraps its sub-plan without changing the
    accumulated scope list."""
    app = Veloce(debug=True, openapi_url=None)
    seen: dict = {}

    def auth(security_scopes: SecurityScopes):
        seen["scopes"] = list(security_scopes.scopes)
        return None

    def wrap(_=Depends(auth)):
        return _

    @app.get("/x")
    async def x(_=Security(wrap, scopes=["only-from-security"])):
        return {}

    await app.handle_request(_req("/x"))
    # The `Depends(auth)` link doesn't push anything new — `auth` sees
    # only the outer Security()'s scopes.
    assert seen["scopes"] == ["only-from-security"]


@pytest.mark.asyncio
async def test_no_security_chain_yields_empty_scopes():
    """A `SecurityScopes` parameter without any Security() above it
    receives an empty list, not None."""
    app = Veloce(debug=True, openapi_url=None)
    seen: dict = {}

    @app.get("/x")
    async def x(security_scopes: SecurityScopes):
        seen["scopes"] = list(security_scopes.scopes)
        return {}

    await app.handle_request(_req("/x"))
    assert seen["scopes"] == []


def test_security_scopes_via_testclient():
    """Sanity: the whole pipeline works end-to-end through TestClient."""
    app = Veloce(debug=True, openapi_url=None)

    def auth(security_scopes: SecurityScopes):
        return {"required_scopes": security_scopes.scope_str}

    @app.get("/me")
    async def me(info=Security(auth, scopes=["users:read", "users:write"])):
        return info

    resp = TestClient(app).get("/me")
    body = resp.json()
    assert body == {"required_scopes": "users:read users:write"}


def test_security_scopes_in_veloce_exports():
    """`from veloce import SecurityScopes` works."""
    from veloce import SecurityScopes as SS

    assert SS is SecurityScopes


class TestSecurityDependency:
    @pytest.mark.asyncio
    async def test_security_with_scopes(self):
        import orjson

        app = Veloce(openapi_url=None)
        oauth2 = OAuth2PasswordBearer(token_url="/token")

        @app.get("/users/me")
        async def me(token=Security(oauth2, scopes=["users:read"])):
            return {"token": token}

        resp = await app.handle_request(
            make_request(path="/users/me", headers={"authorization": "Bearer mytoken"})
        )
        assert resp.status_code == 200
        data = orjson.loads(resp.body)
        assert data["token"] == "mytoken"

    @pytest.mark.asyncio
    async def test_security_inherits_depends(self):
        # Security is a subclass of Depends
        security = HTTPBearer()
        dep = Security(security, scopes=["admin"])
        assert isinstance(dep, Depends)
        assert dep.scopes == ["admin"]
