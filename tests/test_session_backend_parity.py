"""Both session backends are one kind of thing, and answer `permanent` alike.

There was no type meaning "a session middleware", and two defects followed from
that. `security_audit()` asked `isinstance` against the cookie backend alone, so
a server-side-session app passed `veloce check` clean while shipping its session
id over plain HTTP. And the documented `session.permanent` rule - use the longer
lifetime - lived on the cookie backend only, so "remember me" silently did
nothing on the server-side one: users were logged out at the default window
whatever `PERMANENT_SESSION_LIFETIME` said.

`_SessionMiddlewareBase` now carries the type and the lifetime decision, so a
backend added later inherits both without an edit in `app/core.py`.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.middleware.sessions import (
    ServerSessionMiddleware,
    SessionMiddleware,
    _SessionMiddlewareBase,
)
from veloce.sessions import InMemorySessionStore
from veloce.testclient import TestClient

_PERMANENT = 2678400  # 31 days
_DEFAULT = 1209600  # 14 days


class _RecordingStore(InMemorySessionStore):
    """An in-memory store that remembers the TTL it was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.ttls: list[int] = []

    async def write(self, session_id, data, max_age):
        self.ttls.append(max_age)
        await super().write(session_id, data, max_age)

    async def replace(self, session_id, data, max_age):
        self.ttls.append(max_age)
        return await super().replace(session_id, data, max_age)

    async def touch(self, session_id, max_age):
        self.ttls.append(max_age)
        return await super().touch(session_id, max_age)


def _max_age(response) -> int | None:
    for part in response.headers.get("set-cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name.lower() == "max-age":
            return int(value)
    return None


def _app(middleware, *, permanent: bool, **config) -> TestClient:
    app = Veloce(openapi_url=None)
    app.secret_key = "k"
    app.config.update(config)
    app.add_middleware(middleware)

    @app.get("/login")
    async def login(request):
        request.session["user"] = "u"
        request.session.permanent = permanent
        return {"ok": True}

    @app.get("/read")
    async def read(request):
        return {"user": request.session.get("user")}

    return TestClient(app)


# ── One type, so the audit sees every backend ────────────────────────


@pytest.mark.parametrize(
    "middleware",
    [SessionMiddleware(secret_key="k"), ServerSessionMiddleware()],
)
def test_the_audit_flags_an_insecure_cookie_on_either_backend(middleware):
    """The defect: the server-side backend was invisible to `veloce check`."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(middleware)
    assert any("SESSION_COOKIE_SECURE" in w for w in app.security_audit())


@pytest.mark.parametrize(
    "middleware",
    [SessionMiddleware(secret_key="k", secure=True), ServerSessionMiddleware(secure=True)],
)
def test_a_secure_cookie_is_not_flagged_on_either_backend(middleware):
    app = Veloce(openapi_url=None)
    app.config.update({"SECRET_KEY": "k", "SESSION_COOKIE_SECURE": True})
    app.add_middleware(middleware)
    assert not any("SESSION_COOKIE_SECURE" in w for w in app.security_audit())


def test_a_backend_added_later_is_audited_without_touching_the_audit():
    """The scalability property: subclassing is enough."""

    class RedisLikeSessionMiddleware(ServerSessionMiddleware):
        pass

    assert issubclass(RedisLikeSessionMiddleware, _SessionMiddlewareBase)
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(RedisLikeSessionMiddleware())
    assert any("SESSION_COOKIE_SECURE" in w for w in app.security_audit())


# ── One answer to `session.permanent` ────────────────────────────────


def test_the_server_backend_honours_permanent():
    """The defect: it used its default window whatever the config said."""
    client = _app(ServerSessionMiddleware(), permanent=True, PERMANENT_SESSION_LIFETIME=_PERMANENT)
    assert _max_age(client.get("/login")) == _PERMANENT


def test_the_server_backend_still_uses_its_default_when_not_permanent():
    client = _app(ServerSessionMiddleware(), permanent=False, PERMANENT_SESSION_LIFETIME=_PERMANENT)
    assert _max_age(client.get("/login")) == _DEFAULT


@pytest.mark.parametrize("permanent", [False, True])
def test_both_backends_agree_on_the_cookie_lifetime(permanent):
    """One app setting, one answer, whichever backend is installed."""
    cookie = _app(
        SessionMiddleware(secret_key="k", max_age=_DEFAULT),
        permanent=permanent,
        PERMANENT_SESSION_LIFETIME=_PERMANENT,
    )
    server = _app(
        ServerSessionMiddleware(), permanent=permanent, PERMANENT_SESSION_LIFETIME=_PERMANENT
    )
    assert _max_age(cookie.get("/login")) == _max_age(server.get("/login"))


def test_an_explicit_permanent_lifetime_beats_the_config():
    client = _app(
        ServerSessionMiddleware(permanent_lifetime=99),
        permanent=True,
        PERMANENT_SESSION_LIFETIME=_PERMANENT,
    )
    assert _max_age(client.get("/login")) == 99


# ── The store entry lives as long as the cookie says ─────────────────


def test_the_store_ttl_matches_the_permanent_cookie_lifetime():
    """A cookie outliving its entry would log the user out anyway."""
    store = _RecordingStore()
    client = _app(
        ServerSessionMiddleware(store=store),
        permanent=True,
        PERMANENT_SESSION_LIFETIME=_PERMANENT,
    )
    response = client.get("/login")
    assert store.ttls == [_PERMANENT]
    assert _max_age(response) == _PERMANENT


def test_a_renewed_permanent_session_slides_by_the_permanent_lifetime():
    """Sliding expiry must not quietly demote a permanent session."""
    store = _RecordingStore()
    client = _app(
        ServerSessionMiddleware(store=store, renew_on_access=True),
        permanent=True,
        PERMANENT_SESSION_LIFETIME=_PERMANENT,
    )
    client.get("/login")
    store.ttls.clear()
    client.get("/read")
    assert store.ttls == [_PERMANENT]
