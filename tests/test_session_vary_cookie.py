"""Session middleware: Vary: Cookie on session writes + no-persist on 5xx.

`Vary: Cookie` (RFC 9110 Sec. 12.5.5) stops a shared cache keyed on URL alone
from serving one user's session-bearing body to another. A 5xx response should
not persist a half-mutated session (Set-Cookie / store write).
"""

from __future__ import annotations

from veloce import (
    InMemorySessionStore,
    Request,
    Response,
    ServerSessionMiddleware,
    SessionMiddleware,
    Veloce,
)


def _vary_values(resp) -> set[str]:
    """Case-insensitive set of the response's Vary entries."""
    for k, v in resp.headers.items():
        if k.lower() == "vary":
            return {p.strip().lower() for p in v.split(",") if p.strip()}
    return set()


def _has_set_cookie(resp) -> bool:
    return any(k.lower() == "set-cookie" for k in resp.headers)


# ── Cookie SessionMiddleware: Vary ───────────────────────────────────


def _cookie_app(**mw_kwargs) -> Veloce:
    app = Veloce(debug=False, openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, **mw_kwargs))

    @app.get("/write")
    async def write(request: Request):
        request.session["user"] = "alice"
        return {"ok": True}

    @app.get("/read")
    async def read(request: Request):
        return {"user": request.session.get("user")}

    @app.get("/clear")
    async def clear(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/boom")
    async def boom(request: Request):
        request.session["user"] = "alice"
        return Response(500, b"err")

    return app


def test_session_write_emits_vary_cookie():
    client = _cookie_app().test_client()
    resp = client.get("/write")
    assert _has_set_cookie(resp)
    assert "cookie" in _vary_values(resp)


def test_session_clear_emits_vary_cookie():
    client = _cookie_app().test_client()
    client.get("/write")
    resp = client.get("/clear")
    assert "cookie" in _vary_values(resp)


def test_anonymous_read_only_has_no_vary_cookie():
    # No inbound session cookie, handler doesn't mutate -> stays cacheable.
    client = _cookie_app().test_client()
    resp = client.get("/read")
    assert "cookie" not in _vary_values(resp)


def test_vary_on_cookie_opt_out():
    client = _cookie_app(vary_on_cookie=False).test_client()
    resp = client.get("/write")
    assert _has_set_cookie(resp)
    assert "cookie" not in _vary_values(resp)


# ── Cookie SessionMiddleware: no-persist on 5xx ──────────────────────


def test_no_set_cookie_on_5xx():
    client = _cookie_app().test_client()
    resp = client.get("/boom")
    assert resp.status_code == 500
    assert not _has_set_cookie(resp)


def test_persist_on_2xx_and_4xx_only_skips_5xx():
    app = Veloce(debug=False, openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="k" * 32))

    @app.get("/notfound")
    async def notfound(request: Request):
        request.session["user"] = "alice"
        return Response(404, b"nope")

    resp = app.test_client().get("/notfound")
    assert resp.status_code == 404
    assert _has_set_cookie(resp)  # 4xx still persists


def test_persist_on_status_override():
    # Policy fully replaces the default: 503 denied, 500 allowed.
    app = Veloce(debug=False, openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, persist_on_status=lambda s: s != 503))

    @app.get("/503")
    async def err503(request: Request):
        request.session["user"] = "alice"
        return Response(503, b"")

    @app.get("/500")
    async def err500(request: Request):
        request.session["user"] = "alice"
        return Response(500, b"")

    client = app.test_client()
    assert not _has_set_cookie(client.get("/503"))
    assert _has_set_cookie(client.get("/500"))


# ── ServerSessionMiddleware ──────────────────────────────────────────


def _server_app(**mw_kwargs) -> tuple[Veloce, InMemorySessionStore]:
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

    @app.get("/boom")
    async def boom(request: Request):
        request.session["user"] = "alice"
        return Response(500, b"err")

    return app, store


def test_server_session_write_emits_vary_cookie():
    app, _ = _server_app()
    resp = app.test_client().get("/write")
    assert "cookie" in _vary_values(resp)


def test_server_session_clear_emits_vary_cookie():
    app, _ = _server_app()
    client = app.test_client()
    client.get("/write")
    resp = client.get("/clear")
    assert "cookie" in _vary_values(resp)


def test_server_session_not_written_to_store_on_5xx():
    app, store = _server_app()
    resp = app.test_client().get("/boom")
    assert resp.status_code == 500
    assert not _has_set_cookie(resp)
    # The store gained no entry for the failed request.
    assert not store._entries  # InMemorySessionStore keeps payloads in `_entries`


def test_server_session_vary_opt_out():
    app, _ = _server_app(vary_on_cookie=False)
    resp = app.test_client().get("/write")
    assert "cookie" not in _vary_values(resp)
