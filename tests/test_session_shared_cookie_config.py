"""Both session middlewares resolve their shared cookie settings identically.

`SessionMiddleware` and `ServerSessionMiddleware` take the same five cookie
settings, default them the same way, and read the same `SESSION_COOKIE_*` config
keys for any the caller left out. That was two hand-copied implementations, and
the copies had already drifted in how they render SameSite.

They now share `_shared_cookie_settings` and `_overlay_shared_cookie_config`.
These tests hold the two to the same answers so a new shared setting cannot be
wired into one and forgotten in the other.
"""

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


def test_both_defer_the_same_shared_settings_when_none_are_passed():
    first, second = _both()
    assert set(_SHARED) <= first._deferred_settings
    assert set(_SHARED) <= second._deferred_settings


def test_neither_defers_a_setting_that_was_passed():
    first, second = _both(cookie_name="sid", path="/app", httponly=False)
    for middleware in (first, second):
        assert "cookie_name" not in middleware._deferred_settings
        assert "path" not in middleware._deferred_settings
        assert "httponly" not in middleware._deferred_settings
        assert middleware.cookie_name == "sid"
        assert middleware.path == "/app"
        assert middleware.httponly is False


# ── The config overlay ───────────────────────────────────────────────


_CONFIG = {
    "SESSION_COOKIE_NAME": "from_config",
    "APPLICATION_ROOT": "/mounted",
    "SESSION_COOKIE_HTTPONLY": False,
    "SESSION_COOKIE_SECURE": True,
    "SESSION_COOKIE_SAMESITE": "strict",
    "SECRET_KEY": "k",
}


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("cookie_name", "from_config"),
        ("path", "/mounted"),
        ("httponly", False),
        ("secure", True),
        ("samesite", "strict"),
    ],
)
def test_both_read_the_same_config_key_for_a_deferred_setting(setting, expected):
    for middleware in _both():
        _resolve_against(middleware, _CONFIG)
        assert getattr(middleware, setting) == expected, type(middleware).__name__


@pytest.mark.parametrize("setting", _SHARED)
def test_an_explicit_argument_still_beats_config_in_both(setting):
    explicit = {
        "cookie_name": "explicit",
        "path": "/explicit",
        "httponly": False,
        "secure": True,
        "samesite": "none",
    }[setting]
    for middleware in _both(**{setting: explicit}):
        _resolve_against(middleware, _CONFIG)
        assert getattr(middleware, setting) == explicit, type(middleware).__name__


def test_a_string_boolean_from_a_dotenv_file_is_coerced_in_both():
    """`SESSION_COOKIE_SECURE=false` must read as False, not a truthy string."""
    for middleware in _both():
        _resolve_against(middleware, {"SESSION_COOKIE_SECURE": "false", "SECRET_KEY": "k"})
        assert middleware.secure is False, type(middleware).__name__


def test_the_wire_cookie_name_is_rederived_in_both_after_the_overlay():
    for middleware in _both(cookie_prefix="secure", secure=True):
        _resolve_against(middleware, _CONFIG)
        assert middleware._wire_cookie_name == "__Secure-from_config"


# ── The settings only one of them takes ──────────────────────────────


def test_the_signed_cookie_middleware_keeps_its_own_extras():
    middleware = SessionMiddleware(secret_key="k")
    assert {"permanent_lifetime", "max_cookie_size"} <= middleware._deferred_settings
    _resolve_against(
        middleware,
        {"PERMANENT_SESSION_LIFETIME": "600", "MAX_COOKIE_SIZE": "2000", "SECRET_KEY": "k"},
    )
    assert middleware.permanent_lifetime == 600
    assert middleware.max_cookie_size == 2000


def test_the_server_side_middleware_defers_none_of_those():
    middleware = ServerSessionMiddleware()
    assert "permanent_lifetime" not in middleware._deferred_settings
    assert "max_cookie_size" not in middleware._deferred_settings
    assert "secret_key" not in middleware._deferred_settings


def test_a_secret_key_left_out_is_still_taken_from_config():
    middleware = SessionMiddleware()
    assert "secret_key" in middleware._deferred_settings
    _resolve_against(middleware, {"SECRET_KEY": "from-config"})
    assert middleware._signer is not None


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
