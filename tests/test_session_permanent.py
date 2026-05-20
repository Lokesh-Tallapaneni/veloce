"""Session.permanent persistent-session flag (S4)."""

from __future__ import annotations

from veloce import Request, Session, Veloce
from veloce.middleware.sessions import SessionMiddleware
from veloce.testclient import TestClient

_DEFAULT_MAX_AGE = 86400 * 14
_PERMANENT_LIFETIME = 86400 * 31


def test_permanent_defaults_false():
    s = Session()
    assert s.permanent is False


def test_setting_permanent_marks_modified():
    s = Session()
    s.permanent = True
    assert s.permanent is True
    assert s.modified is True


def test_permanent_backed_by_reserved_key():
    s = Session()
    s.permanent = True
    assert s.get("_permanent") is True


def test_permanent_restored_from_loaded_data():
    s = Session({"_permanent": True, "user": "alice"})
    assert s.permanent is True
    assert s.modified is False


def test_permanent_session_cookie_uses_long_lifetime():
    app = Veloce()
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)

    @app.get("/x")
    async def x(request: Request):
        request.session.permanent = True
        request.session["user"] = "alice"
        return {}

    with TestClient(app) as client:
        resp = client.get("/x")

    cookie = resp.headers.get("set-cookie", "")
    assert f"Max-Age={_PERMANENT_LIFETIME}" in cookie


def test_non_permanent_session_cookie_uses_default_lifetime():
    app = Veloce()
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)

    @app.get("/x")
    async def x(request: Request):
        request.session["user"] = "alice"
        return {}

    with TestClient(app) as client:
        resp = client.get("/x")

    cookie = resp.headers.get("set-cookie", "")
    assert f"Max-Age={_DEFAULT_MAX_AGE}" in cookie


def test_permanent_persists_across_requests():
    app = Veloce()
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)
    seen: list = []

    @app.get("/set")
    async def setter(request: Request):
        request.session.permanent = True
        request.session["user"] = "alice"
        return {}

    @app.get("/read")
    async def reader(request: Request):
        seen.append(request.session.permanent)
        return {}

    with TestClient(app) as client:
        client.get("/set")
        client.get("/read")

    assert seen == [True]
