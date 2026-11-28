"""Sliding session expiry - renew TTL/Max-Age on a read-only access.

With `renew_on_access=True` an existing session that was merely *read* during
a request has its expiry refreshed on the way out: the cookie middleware
re-signs the cookie (new `Max-Age`), and the server-side middleware refreshes
the store TTL plus re-stamps the cookie. Default (off) leaves the prior
behavior - only a modifying write writes anything back.
"""

from __future__ import annotations

import veloce.sessions as _sessions
from veloce import (
    InMemorySessionStore,
    Request,
    Response,
    ServerSessionMiddleware,
    Veloce,
)
from veloce.middleware.sessions import SessionMiddleware
from veloce.testclient import TestClient

_SECRET = "k" * 32


# ── store.touch primitive ─────────────────────────────────────────────


async def test_store_touch_refreshes_ttl_without_rewriting_payload():
    store = InMemorySessionStore()
    await store.write("sid", {"user": "alice"}, max_age=10)
    before = store.expires_at("sid")

    assert await store.touch("sid", max_age=1000) is True
    after = store.expires_at("sid")
    assert before is not None and after is not None
    assert after > before
    # Payload unchanged - which is why the expiry is what must be observed.
    assert await store.read("sid") == {"user": "alice"}


async def test_store_touch_returns_false_for_absent_id():
    store = InMemorySessionStore()
    assert await store.touch("missing", max_age=1000) is False


async def test_store_touch_treats_expired_as_absent():
    store = InMemorySessionStore()
    await store.write("sid", {"user": "alice"}, max_age=-1)  # already expired
    assert await store.touch("sid", max_age=1000) is False
    assert "sid" not in store


# ── cookie SessionMiddleware ──────────────────────────────────────────


def _cookie_app(renew_on_access: bool) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=_SECRET, renew_on_access=renew_on_access)

    @app.post("/login")
    async def login(request: Request):
        request.session["user"] = "alice"
        return {}

    @app.get("/read")
    async def read(request: Request):
        return {"user": request.session.get("user")}

    @app.get("/notouch")
    async def notouch(request: Request):
        # Never touches request.session.
        return {}

    return app


def test_cookie_read_only_does_not_resign_when_off():
    app = _cookie_app(renew_on_access=False)
    with TestClient(app) as client:
        client.post("/login")
        resp = client.get("/read")
    # Default: a read-only access writes no Set-Cookie.
    assert "set-cookie" not in {k.lower() for k in resp.headers}


def test_cookie_read_only_resigns_when_on():
    app = _cookie_app(renew_on_access=True)
    with TestClient(app) as client:
        client.post("/login")
        resp = client.get("/read")
    cookie = resp.headers.get("set-cookie", "")
    # Sliding expiry re-signs the cookie on a read-only access.
    assert "session=" in cookie
    assert "Max-Age=" in cookie


def test_cookie_untouched_session_never_resigns():
    app = _cookie_app(renew_on_access=True)
    with TestClient(app) as client:
        client.post("/login")
        resp = client.get("/notouch")
    # Handler never accessed the session -> nothing slides forward.
    assert "set-cookie" not in {k.lower() for k in resp.headers}


def test_cookie_new_empty_session_not_written_on_renew():
    app = _cookie_app(renew_on_access=True)
    with TestClient(app) as client:
        # No prior login: a fresh client reading an empty session.
        resp = client.get("/read")
    assert resp.json() == {"user": None}
    assert "set-cookie" not in {k.lower() for k in resp.headers}


# ── server-side ServerSessionMiddleware ───────────────────────────────


def _server_app(renew_on_access: bool) -> tuple[Veloce, InMemorySessionStore]:
    store = InMemorySessionStore()
    app = Veloce(openapi_url=None)
    app.add_middleware(ServerSessionMiddleware, store=store, renew_on_access=renew_on_access)

    @app.post("/login")
    async def login(request: Request):
        request.session["user"] = "alice"
        return {}

    @app.get("/read")
    async def read(request: Request):
        return {"user": request.session.get("user")}

    @app.get("/notouch")
    async def notouch(request: Request):
        return {}

    return app, store


def test_server_read_only_does_not_restamp_when_off():
    app, store = _server_app(renew_on_access=False)
    with TestClient(app) as client:
        client.post("/login")
        (sid,) = store
        before = store.expires_at(sid)
        resp = client.get("/read")
    # Default: no cookie re-stamp and store TTL unchanged.
    assert "set-cookie" not in {k.lower() for k in resp.headers}
    after = store.expires_at(sid)
    assert after == before


def test_server_read_only_restamps_when_on(monkeypatch):
    # `time.time()` can return the same value twice within one OS clock tick
    # (notably on Windows), which would make the restamp land on the identical
    # expiry. Drive a strictly increasing clock so the slide-forward is
    # deterministic regardless of platform resolution.

    clock = {"t": 1000.0}

    def _tick() -> float:
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(_sessions.time, "time", _tick)

    app, store = _server_app(renew_on_access=True)
    with TestClient(app) as client:
        client.post("/login")
        (sid,) = store
        before = store.expires_at(sid)
        resp = client.get("/read")
    cookie = resp.headers.get("set-cookie", "")
    # Cookie re-stamped and store TTL slid forward.
    assert "session=" in cookie
    after = store.expires_at(sid)
    assert after > before
    # Same id - sliding expiry never rotates the session id.
    assert sid in store


def test_server_untouched_session_never_restamps():
    app, store = _server_app(renew_on_access=True)
    with TestClient(app) as client:
        client.post("/login")
        (sid,) = store
        before = store.expires_at(sid)
        resp = client.get("/notouch")
    assert "set-cookie" not in {k.lower() for k in resp.headers}
    after = store.expires_at(sid)
    assert after == before


def test_server_revoked_before_read_is_treated_as_new():
    app, store = _server_app(renew_on_access=True)
    with TestClient(app) as client:
        client.post("/login")
        # Revoke server-side between requests: the cookie id no longer
        # resolves, so process_request loads a fresh (new) session.
        store.clear()
        resp = client.get("/read")
    # A new empty session that was only read is never written back, so no
    # cookie re-stamp and no resurrection of the revoked entry.
    assert resp.json() == {"user": None}
    assert "set-cookie" not in {k.lower() for k in resp.headers}
    assert len(store) == 0


async def test_renew_clears_cookie_when_revoked_under_us():
    # Direct exercise of the concurrent-revocation race: a session that
    # resolved on read but whose store entry vanished before `_renew` runs.
    store = InMemorySessionStore()
    mw = ServerSessionMiddleware(store=store, renew_on_access=True)
    request = Request("GET", "/", "", [], b"")
    request.state["_session_id"] = "ghost"  # id with no store entry
    response = Response()
    await mw._renew(request, response)
    rendered = response.headers.get("Set-Cookie", "")
    assert "session=" in rendered
    assert "Max-Age=0" in rendered or "Expires=" in rendered
