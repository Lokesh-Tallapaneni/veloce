"""`session` context-local proxy (Q4)."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce, session
from veloce.testclient import TestClient


def test_session_proxy_raises_outside_context():
    with pytest.raises(RuntimeError, match="outside of request context"):
        _ = session["key"]


def test_session_proxy_falsy_outside_context():
    assert not session


def test_session_proxy_resolves_during_dispatch():
    app = Veloce()
    seen: dict = {}

    @app.get("/x")
    async def x(req: Request):
        # Seed a session dict on the request state directly.
        req._state["session"] = {"user": "alice"}
        seen["user"] = session["user"]
        seen["has"] = "user" in session
        return {}

    with TestClient(app) as client:
        client.get("/x")

    assert seen == {"user": "alice", "has": True}


def test_session_proxy_setitem():
    app = Veloce()
    captured: dict = {}

    @app.get("/x")
    async def x(req: Request):
        req._state["session"] = {}
        session["count"] = 7
        captured["count"] = req._state["session"]["count"]
        return {}

    with TestClient(app) as client:
        client.get("/x")

    assert captured["count"] == 7


def test_session_proxy_raises_without_middleware():
    app = Veloce()
    errors: list = []

    @app.get("/x")
    async def x(req: Request):
        try:
            _ = session["k"]
        except RuntimeError as e:
            errors.append(str(e))
        return {}

    with TestClient(app) as client:
        client.get("/x")

    # No SessionMiddleware → Request.session raises, proxy propagates it.
    assert errors and "SessionMiddleware" in errors[0]
