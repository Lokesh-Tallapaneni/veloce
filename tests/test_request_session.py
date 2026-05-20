"""Request.session property tests (S5)."""

from __future__ import annotations

import pytest

from veloce import Request, SessionMiddleware, Veloce


def _req(path: str = "/") -> Request:
    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


# ── Without SessionMiddleware ─────────────────────────────────────────


def test_session_unavailable_raises_runtime_error():
    """`request.session` without the middleware raises a clear error."""
    req = _req()
    with pytest.raises(RuntimeError, match="SessionMiddleware"):
        _ = req.session


# ── With SessionMiddleware ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_is_writable_dict_inside_handler():
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="x" * 32))

    captured: dict = {}

    @app.get("/x")
    async def x(request: Request):
        # Session starts as an empty dict (or whatever's in the cookie).
        captured["initial"] = dict(request.session)
        request.session["user"] = "alice"
        return {"ok": True}

    await app.handle_request(_req("/x"))
    assert captured["initial"] == {}


@pytest.mark.asyncio
async def test_session_mutations_are_visible_via_state():
    """The property and `_state["session"]` are the same dict — mutating
    one shows up in the other."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="y" * 32))

    captured: dict = {}

    @app.get("/x")
    async def x(request: Request):
        request.session["k"] = "v"
        captured["state"] = dict(request._state.get("session", {}))
        return {}

    await app.handle_request(_req("/x"))
    assert captured["state"] == {"k": "v"}


def test_session_round_trip_via_signed_cookie():
    """Set a value on request 1, read it back on request 2."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="z" * 32))

    @app.get("/set")
    async def set_it(request: Request):
        request.session["count"] = 7
        return {"ok": True}

    @app.get("/get")
    async def get_it(request: Request):
        return {"count": request.session.get("count")}

    from veloce.testclient import TestClient

    client = TestClient(app)
    client.get("/set")
    resp = client.get("/get")
    assert resp.json() == {"count": 7}
