"""`Vary: Cookie` on session access — cache-leak protection (S1).

A response built from session-personalised state must carry `Vary: Cookie`
so a shared/CDN cache does not serve one user's body to another.
"""

from __future__ import annotations

from veloce import Request, Veloce, suppress_session_vary
from veloce.middleware.sessions import ServerSessionMiddleware, SessionMiddleware
from veloce.testclient import TestClient


def _vary(resp) -> set[str]:
    raw = resp.headers.get("vary", "")
    return {tok.strip().lower() for tok in raw.split(",") if tok.strip()}


def test_reading_session_adds_vary_cookie():
    app = Veloce()
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)

    @app.get("/read")
    async def read(request: Request):
        # A read, not a write — the session is unmodified.
        _ = request.session.get("user_id")
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/read")

    assert "cookie" in _vary(resp)


def test_untouched_session_has_no_vary():
    app = Veloce()
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)

    @app.get("/plain")
    async def plain(request: Request):
        # Never touches the session at all.
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/plain")

    assert "cookie" not in _vary(resp)


def test_writing_session_adds_vary_cookie():
    app = Veloce()
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)

    @app.get("/write")
    async def write(request: Request):
        request.session["count"] = 1
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/write")

    assert "cookie" in _vary(resp)


def test_suppress_session_vary_opt_out():
    app = Veloce()
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)

    @app.get("/asset")
    async def asset(request: Request):
        _ = request.session.get("user_id")
        suppress_session_vary(request)
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/asset")

    assert "cookie" not in _vary(resp)


def test_vary_merges_with_existing_value():
    app = Veloce()
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)

    @app.get("/read")
    async def read(request: Request):
        from veloce import JSONResponse

        resp = JSONResponse({"ok": True})
        resp.add_vary("Accept-Encoding")
        _ = request.session.get("user_id")
        return resp

    with TestClient(app) as client:
        resp = client.get("/read")

    assert _vary(resp) == {"accept-encoding", "cookie"}


def test_server_session_read_adds_vary_cookie():
    app = Veloce()
    app.add_middleware(ServerSessionMiddleware)

    @app.get("/read")
    async def read(request: Request):
        _ = request.session.get("user_id")
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/read")

    assert "cookie" in _vary(resp)


def test_server_session_untouched_has_no_vary():
    app = Veloce()
    app.add_middleware(ServerSessionMiddleware)

    @app.get("/plain")
    async def plain(request: Request):
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/plain")

    assert "cookie" not in _vary(resp)
