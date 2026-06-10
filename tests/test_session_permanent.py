"""Session.permanent persistent-session flag (S4)."""

from __future__ import annotations

import time

from veloce import Request, Session, Veloce
from veloce.middleware.sessions import SessionMiddleware
from veloce.testclient import TestClient

_DEFAULT_MAX_AGE = 86400 * 14
_PERMANENT_LIFETIME = 86400 * 31


def _sign_aged_cookie(mw: SessionMiddleware, payload: dict, age_seconds: int) -> str:
    """Sign a session payload as if it were issued `age_seconds` ago.

    Replay safety, not the client `Max-Age`, is what the server must enforce:
    an attacker keeps the stolen cookie regardless of its browser expiry.
    """
    import veloce.signing as signing

    real_time = time.time
    try:
        signing.time.time = lambda: real_time() - age_seconds  # type: ignore[attr-defined]
        return mw._signer.dumps(payload)
    finally:
        signing.time.time = real_time  # type: ignore[attr-defined]


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


def test_stale_non_permanent_cookie_rejected_past_max_age():
    """A non-permanent cookie older than max_age does not replay server-side."""
    app = Veloce()
    mw = SessionMiddleware(secret_key="k" * 32)
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)
    seen: list = []

    @app.get("/read")
    async def reader(request: Request):
        seen.append((request.session.new, dict(request.session)))
        return {}

    # Aged past the 14-day max_age but still within the 31-day permanent
    # window the read uses to avoid rejecting permanent cookies.
    aged = _sign_aged_cookie(mw, {"user": "alice"}, _DEFAULT_MAX_AGE + 3600)
    with TestClient(app) as client:
        client.cookies["session"] = aged
        client.get("/read")

    new, data = seen[0]
    assert new is True
    assert data == {}


def test_fresh_non_permanent_cookie_within_max_age_accepted():
    """A non-permanent cookie younger than max_age still loads normally."""
    app = Veloce()
    mw = SessionMiddleware(secret_key="k" * 32)
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)
    seen: list = []

    @app.get("/read")
    async def reader(request: Request):
        seen.append((request.session.new, dict(request.session)))
        return {}

    aged = _sign_aged_cookie(mw, {"user": "alice"}, _DEFAULT_MAX_AGE - 3600)
    with TestClient(app) as client:
        client.cookies["session"] = aged
        client.get("/read")

    new, data = seen[0]
    assert new is False
    assert data == {"user": "alice"}


def test_permanent_cookie_within_permanent_lifetime_accepted():
    """A permanent cookie older than max_age but within permanent_lifetime loads."""
    app = Veloce()
    mw = SessionMiddleware(secret_key="k" * 32)
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)
    seen: list = []

    @app.get("/read")
    async def reader(request: Request):
        seen.append((request.session.new, request.session.permanent))
        return {}

    aged = _sign_aged_cookie(mw, {"user": "alice", "_permanent": True}, _DEFAULT_MAX_AGE + 3600)
    with TestClient(app) as client:
        client.cookies["session"] = aged
        client.get("/read")

    new, permanent = seen[0]
    assert new is False
    assert permanent is True


def test_stale_permanent_cookie_rejected_when_permanent_lifetime_shorter():
    """With permanent_lifetime < max_age, a permanent cookie older than
    permanent_lifetime is rejected: the ceiling follows the `_permanent` flag,
    not whichever configured value is larger."""
    short_permanent = 86400 * 2
    long_max_age = 86400 * 30
    app = Veloce()
    mw = SessionMiddleware(
        secret_key="k" * 32, max_age=long_max_age, permanent_lifetime=short_permanent
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key="k" * 32,
        max_age=long_max_age,
        permanent_lifetime=short_permanent,
    )
    seen: list = []

    @app.get("/read")
    async def reader(request: Request):
        seen.append((request.session.new, dict(request.session)))
        return {}

    # Permanent, older than the 2-day permanent_lifetime but younger than the
    # 30-day max_age: must NOT replay despite max_age being larger.
    aged = _sign_aged_cookie(mw, {"user": "alice", "_permanent": True}, short_permanent + 3600)
    with TestClient(app) as client:
        client.cookies["session"] = aged
        client.get("/read")

    new, data = seen[0]
    assert new is True
    assert data == {}
