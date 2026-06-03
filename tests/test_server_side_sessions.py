"""F2 — server-side session backend with revocation.

`ServerSessionMiddleware` keeps the session payload in a `SessionStore`;
the cookie carries only an opaque session id, so a session can be
revoked server-side and a tampered cookie cannot forge one.
"""

from __future__ import annotations

import asyncio

from veloce import (
    InMemorySessionStore,
    Request,
    ServerSessionMiddleware,
    SessionStore,
    Veloce,
)


def _session_app() -> tuple[Veloce, InMemorySessionStore]:
    store = InMemorySessionStore()
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(ServerSessionMiddleware(store=store))

    @app.post("/login")
    async def login(request: Request):
        request.session["user"] = "alice"
        return {"ok": True}

    @app.get("/whoami")
    async def whoami(request: Request):
        return {"user": request.session.get("user")}

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    return app, store


# ── round-trip ────────────────────────────────────────────────────────


def test_session_round_trips_across_requests():
    app, _ = _session_app()
    client = app.test_client()

    client.post("/login")
    resp = client.get("/whoami")
    assert resp.json() == {"user": "alice"}


def test_separate_clients_get_separate_sessions():
    app, _ = _session_app()
    c1 = app.test_client()
    c2 = app.test_client()

    c1.post("/login")
    assert c1.get("/whoami").json() == {"user": "alice"}
    # A second client never logged in — it has no session.
    assert c2.get("/whoami").json() == {"user": None}


# ── the cookie is an opaque id, not the payload ───────────────────────


def test_cookie_carries_only_an_opaque_id():
    app, store = _session_app()
    client = app.test_client()

    resp = client.post("/login")
    set_cookie = resp.headers.get("Set-Cookie", "")
    # The session value never appears in the cookie — only an id does.
    assert "alice" not in set_cookie
    # ...and the payload is what actually lives in the store.
    assert any("alice" in str(v) for v in store._entries.values())


# ── revocation ────────────────────────────────────────────────────────


def test_clearing_the_session_revokes_it():
    app, store = _session_app()
    client = app.test_client()

    client.post("/login")
    assert len(store._entries) == 1

    client.post("/logout")
    # The store entry is gone — the session was revoked server-side.
    assert len(store._entries) == 0
    assert client.get("/whoami").json() == {"user": None}


def test_store_delete_revokes_a_session_by_id():
    app, store = _session_app()
    client = app.test_client()
    client.post("/login")

    # An admin revokes the session straight from the store.
    session_id = next(iter(store._entries))
    asyncio.run(store.delete(session_id))

    assert client.get("/whoami").json() == {"user": None}


def test_unknown_cookie_yields_a_fresh_session():
    app, _ = _session_app()
    client = app.test_client()
    client.cookies["session"] = "this-id-was-never-issued"

    assert client.get("/whoami").json() == {"user": None}


def test_unmodified_session_sets_no_cookie():
    app, _ = _session_app()
    client = app.test_client()

    resp = client.get("/whoami")  # reads, never writes
    assert "Set-Cookie" not in resp.headers


def test_emptying_a_never_stored_session_sets_no_cookie():
    """A handler that touches then leaves a brand-new session empty must
    not emit a cookie or leave an orphan store entry."""
    store = InMemorySessionStore()
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(ServerSessionMiddleware(store=store))

    @app.post("/noop")
    async def noop(request: Request):
        request.session.clear()  # empty, and there was no prior cookie
        return {"ok": True}

    resp = app.test_client().post("/noop")
    assert "Set-Cookie" not in resp.headers
    assert len(store._entries) == 0


# ── session-id rotation (fixation defence) ────────────────────────────


def test_regenerate_id_rotates_the_session_id():
    """`session.regenerate_id()` at a privilege boundary mints a fresh id
    and drops the old one, carrying the payload across."""
    store = InMemorySessionStore()
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(ServerSessionMiddleware(store=store))

    @app.post("/visit")
    async def visit(request: Request):
        request.session["seen"] = True
        return {"ok": True}

    @app.post("/login")
    async def login(request: Request):
        request.session["user"] = "alice"
        request.session.regenerate_id()
        return {"ok": True}

    @app.get("/whoami")
    async def whoami(request: Request):
        return {
            "user": request.session.get("user"),
            "seen": request.session.get("seen"),
        }

    client = app.test_client()
    client.post("/visit")
    old_id = next(iter(store._entries))

    client.post("/login")
    # The pre-login id no longer resolves; exactly one fresh id replaced it.
    assert old_id not in store._entries
    assert len(store._entries) == 1
    assert next(iter(store._entries)) != old_id
    # The payload survived the rotation.
    assert client.get("/whoami").json() == {"user": "alice", "seen": True}


# ── the store itself ──────────────────────────────────────────────────


async def test_in_memory_store_read_write_delete():
    store = InMemorySessionStore()
    await store.write("sid-1", {"k": "v"}, max_age=60)
    assert await store.read("sid-1") == {"k": "v"}

    await store.delete("sid-1")
    assert await store.read("sid-1") is None


async def test_in_memory_store_expires_entries():
    store = InMemorySessionStore()
    await store.write("sid-2", {"k": "v"}, max_age=0)  # already expired
    assert await store.read("sid-2") is None
    # The expired entry was evicted, not just hidden.
    assert "sid-2" not in store._entries


async def test_in_memory_store_returns_a_copy():
    """Mutating a read payload must not corrupt the stored copy."""
    store = InMemorySessionStore()
    await store.write("sid-3", {"items": [1]}, max_age=60)
    got = await store.read("sid-3")
    got["items"] = "tampered"
    assert (await store.read("sid-3")) == {"items": [1]}


def test_in_memory_store_is_a_session_store():
    assert isinstance(InMemorySessionStore(), SessionStore)


# ── conditional write (replace) — revocation race ─────────────────────


async def test_in_memory_store_replace_only_writes_when_present():
    """`replace` is the race-safe write: it updates an existing id and
    reports success, but refuses to (re)create an absent one."""
    store = InMemorySessionStore()

    # An absent id is not resurrected.
    assert await store.replace("ghost", {"k": "v"}, max_age=60) is False
    assert await store.read("ghost") is None

    await store.write("sid", {"k": "v1"}, max_age=60)
    # A present id is updated.
    assert await store.replace("sid", {"k": "v2"}, max_age=60) is True
    assert await store.read("sid") == {"k": "v2"}

    # Once deleted, replace no longer succeeds.
    await store.delete("sid")
    assert await store.replace("sid", {"k": "v3"}, max_age=60) is False
    assert await store.read("sid") is None


async def test_in_memory_store_replace_treats_an_expired_entry_as_absent():
    """An entry past its TTL but not yet lazily evicted must not be
    revived by `replace` — a stale session stays dead."""
    store = InMemorySessionStore()
    await store.write("sid", {"k": "v"}, max_age=0)  # already expired
    # The entry is still physically present (nothing has read/deleted it)...
    assert "sid" in store._entries
    # ...but replace treats it as absent and does not resurrect it.
    assert await store.replace("sid", {"k": "fresh"}, max_age=60) is False
    assert await store.read("sid") is None


async def test_base_session_store_replace_default_is_conditional():
    """The base-class `replace` default (read-then-write) writes only when
    the id still exists, so a custom store inherits the race-safe contract
    without implementing its own conditional write."""

    class DictStore(SessionStore):
        def __init__(self) -> None:
            self.data: dict[str, dict] = {}

        async def read(self, session_id):
            entry = self.data.get(session_id)
            return dict(entry) if entry is not None else None

        async def write(self, session_id, data, max_age):
            self.data[session_id] = dict(data)

        async def delete(self, session_id):
            self.data.pop(session_id, None)

    store = DictStore()
    assert await store.replace("missing", {"x": 1}, max_age=60) is False
    assert await store.read("missing") is None

    await store.write("sid", {"x": 1}, max_age=60)
    assert await store.replace("sid", {"x": 2}, max_age=60) is True
    assert await store.read("sid") == {"x": 2}


def test_session_revoked_mid_request_is_not_resurrected():
    """A session deleted from the store while a request is in flight must
    not be written back: the in-flight `process_response` honours the
    revocation and drops the cookie instead of resurrecting the entry."""
    store = InMemorySessionStore()
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(ServerSessionMiddleware(store=store))

    @app.post("/login")
    async def login(request: Request):
        request.session["user"] = "alice"
        return {"ok": True}

    @app.post("/touch-while-revoked")
    async def touch(request: Request):
        # Mutate the session, then simulate a concurrent revocation by
        # deleting the underlying store entry before the response runs.
        request.session["count"] = 1
        await store.delete(request._state["_session_id"])
        return {"ok": True}

    @app.get("/whoami")
    async def whoami(request: Request):
        return {"user": request.session.get("user")}

    client = app.test_client()
    client.post("/login")
    assert len(store._entries) == 1

    resp = client.post("/touch-while-revoked")
    # The revoked session was not written back — no orphan store entry.
    assert len(store._entries) == 0
    # The client is told to drop its now-dead cookie.
    assert "Max-Age=0" in resp.headers.get("Set-Cookie", "")
    # A follow-up request sees no session.
    assert client.get("/whoami").json() == {"user": None}


def test_modifying_a_live_session_still_writes_back():
    """The conditional write must not break the normal path: a session
    that is still present in the store is updated as before."""
    store = InMemorySessionStore()
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(ServerSessionMiddleware(store=store))

    @app.post("/login")
    async def login(request: Request):
        request.session["hits"] = 1
        return {"ok": True}

    @app.post("/bump")
    async def bump(request: Request):
        request.session["hits"] = request.session.get("hits", 0) + 1
        return {"hits": request.session["hits"]}

    client = app.test_client()
    client.post("/login")
    assert client.post("/bump").json() == {"hits": 2}
    assert client.post("/bump").json() == {"hits": 3}
    # Still exactly one live session entry.
    assert len(store._entries) == 1


# ── Cookie Domain attribute ───────────────────────────────────────────


def _domain_server_app(**mw_kwargs) -> Veloce:
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


def _set_cookie_line(resp) -> str:
    for k, v in resp.headers.items():
        if k.lower() == "set-cookie":
            return v
    return ""


def test_server_session_cookie_includes_domain():
    client = _domain_server_app(domain=".example.com").test_client()
    resp = client.get("/write")
    assert "Domain=.example.com" in _set_cookie_line(resp)


def test_server_session_clear_includes_domain():
    client = _domain_server_app(domain=".example.com").test_client()
    client.get("/write")
    resp = client.get("/clear")
    line = _set_cookie_line(resp)
    assert "Domain=.example.com" in line
    assert "Max-Age=0" in line


def test_server_session_domain_insecure_samesite_none_warns():
    import pytest

    with pytest.warns(UserWarning):
        ServerSessionMiddleware(domain=".example.com", secure=False, samesite="none")
