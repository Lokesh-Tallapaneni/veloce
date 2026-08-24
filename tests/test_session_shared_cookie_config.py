"""The cookie settings both session middlewares share, taken from their constructors."""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.middleware.sessions import (
    _SHARED_COOKIE_DEFAULTS,
    ServerSessionMiddleware,
    SessionMiddleware,
)
from veloce.testclient import TestClient

#: The settings both constructors take. Every test below runs against both.
_SHARED = ["cookie_name", "path", "httponly", "secure", "samesite"]


def _both(**kwargs):
    """One instance of each middleware, built with the same arguments."""
    return [
        SessionMiddleware(secret_key="k", **kwargs),
        ServerSessionMiddleware(**kwargs),
    ]


def _resolve_against(middleware, config: dict) -> None:
    """Drive one request through `middleware` so its deferred config resolves."""
    app = Veloce(openapi_url=None)
    app.config.update(config)

    @app.get("/")
    async def index() -> dict:
        return {"ok": True}

    app.add_middleware(middleware)
    TestClient(app).get("/")


# ── The defaults ─────────────────────────────────────────────────────


@pytest.mark.parametrize("setting", _SHARED)
def test_both_default_a_shared_setting_the_same_way(setting):
    first, second = _both()
    assert getattr(first, setting) == getattr(second, setting)
    assert getattr(first, setting) == _SHARED_COOKIE_DEFAULTS[setting]


# ── The config overlay ───────────────────────────────────────────────


_CONFIG = {
    "SESSION_COOKIE_NAME": "from_config",
    "APPLICATION_ROOT": "/mounted",
    "SESSION_COOKIE_HTTPONLY": False,
    "SESSION_COOKIE_SECURE": True,
    "SESSION_COOKIE_SAMESITE": "strict",
    "SECRET_KEY": "k",
}


# ── The settings only one of them takes ──────────────────────────────


def test_a_secret_key_left_out_is_still_taken_from_config():
    """The one setting still settled against the app: it is the app's key, not
    an attribute of this cookie, and `app.secret_key` is already its only home."""
    middleware = SessionMiddleware()
    assert middleware._pending_config
    _resolve_against(middleware, {"SECRET_KEY": "from-config"})
    assert middleware._signer is not None
    assert not middleware._pending_config


def test_a_cookie_setting_left_out_takes_the_library_default():
    """No app is consulted: the constructor and the defaults settle it alone."""
    middleware = SessionMiddleware(secret_key="k")
    assert middleware.secure is False
    assert middleware.cookie_name == "session"
    assert middleware.httponly is True
    assert middleware.samesite == "lax"


# ── The invariants the shared validation still enforces ──────────────


@pytest.mark.parametrize("factory", [SessionMiddleware, ServerSessionMiddleware])
def test_a_host_prefix_still_requires_a_root_path_in_both(factory):
    kwargs = {"secret_key": "k"} if factory is SessionMiddleware else {}
    with pytest.raises(ValueError, match="requires path='/'"):
        factory(cookie_prefix="host", secure=True, path="/sub", **kwargs)


@pytest.mark.parametrize("factory", [SessionMiddleware, ServerSessionMiddleware])
def test_a_partitioned_cookie_still_requires_secure_and_samesite_none(factory):
    kwargs = {"secret_key": "k"} if factory is SessionMiddleware else {}
    with pytest.raises(ValueError, match="partitioned=True"):
        factory(partitioned=True, secure=False, **kwargs)


def test_the_session_still_round_trips_through_a_real_request():
    """The refactor is config wiring; the cookie must still work."""
    app = Veloce(openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="k"))

    @app.get("/set")
    async def set_value(request) -> dict:
        request.session["v"] = 1
        return {"ok": True}

    @app.get("/get")
    async def get_value(request) -> dict:
        return {"v": request.session.get("v")}

    client = TestClient(app)
    client.get("/set")
    assert client.get("/get").json() == {"v": 1}
