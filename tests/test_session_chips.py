"""Session middleware CHIPS (`Partitioned`) + `Domain` plumbing.

A partitioned session cookie (`Partitioned`, requiring `Secure` and
`SameSite=None`) is keyed to the embedding top-level site, so an embedded
third-party context gets an isolated session jar. Misconfiguration is
rejected at construction (fail-fast) rather than silently dropped.
"""

from __future__ import annotations

import pytest

from veloce import (
    InMemorySessionStore,
    Request,
    ServerSessionMiddleware,
    SessionMiddleware,
    Veloce,
)


def _set_cookie_line(resp) -> str:
    for k, v in resp.headers.items():
        if k.lower() == "set-cookie":
            return v
    return ""


def _cookie_app(**mw_kwargs) -> Veloce:
    app = Veloce(debug=False, openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, **mw_kwargs))

    @app.get("/write")
    async def write(request: Request):
        request.session["user"] = "alice"
        return {"ok": True}

    @app.get("/clear")
    async def clear(request: Request):
        request.session.clear()
        return {"ok": True}

    return app


def _server_app(**mw_kwargs) -> Veloce:
    store = InMemorySessionStore()
    app = Veloce(debug=False, openapi_url=None)
    app.add_middleware(ServerSessionMiddleware(store=store, **mw_kwargs))

    @app.get("/write")
    async def write(request: Request):
        request.session["user"] = "alice"
        return {"ok": True}

    @app.get("/clear")
    async def clear(request: Request):
        request.session.clear()
        return {"ok": True}

    return app


# ── Cookie-based SessionMiddleware ───────────────────────────────────


def test_cookie_session_partitioned_and_domain_on_write():
    client = _cookie_app(
        secure=True, samesite="none", partitioned=True, domain="example.com"
    ).test_client()
    line = _set_cookie_line(client.get("/write"))
    assert "Domain=example.com" in line
    assert "Partitioned" in line


def test_cookie_session_partitioned_and_domain_on_clear():
    client = _cookie_app(
        secure=True, samesite="none", partitioned=True, domain="example.com"
    ).test_client()
    client.get("/write")
    line = _set_cookie_line(client.get("/clear"))
    assert "Max-Age=0" in line
    assert "Partitioned" in line
    assert "Domain=example.com" in line


# ── ServerSessionMiddleware ──────────────────────────────────────────


def test_server_session_partitioned_and_domain_on_write():
    client = _server_app(
        secure=True, samesite="none", partitioned=True, domain="example.com"
    ).test_client()
    line = _set_cookie_line(client.get("/write"))
    assert "Domain=example.com" in line
    assert "Partitioned" in line


def test_server_session_partitioned_and_domain_on_clear():
    client = _server_app(
        secure=True, samesite="none", partitioned=True, domain="example.com"
    ).test_client()
    client.get("/write")
    line = _set_cookie_line(client.get("/clear"))
    assert "Max-Age=0" in line
    assert "Partitioned" in line
    assert "Domain=example.com" in line


# ── Construction-time fail-fast guards ───────────────────────────────


def test_cookie_session_partitioned_requires_secure():
    with pytest.raises(ValueError):
        SessionMiddleware(secret_key="k" * 32, partitioned=True)


def test_cookie_session_partitioned_requires_samesite_none():
    with pytest.raises(ValueError):
        SessionMiddleware(secret_key="k" * 32, secure=True, samesite="lax", partitioned=True)


def test_server_session_partitioned_requires_secure():
    with pytest.raises(ValueError):
        ServerSessionMiddleware(partitioned=True)
