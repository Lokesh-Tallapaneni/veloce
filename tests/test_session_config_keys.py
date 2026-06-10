"""Session middleware settings resolved from app.config (SECRET_KEY, SESSION_COOKIE_*)."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce
from veloce.middleware.sessions import ServerSessionMiddleware, SessionMiddleware
from veloce.testclient import TestClient


def _add_session_routes(app: Veloce) -> None:
    @app.get("/set")
    async def set_value(request: Request):
        request.session["who"] = "alice"
        return {}

    @app.get("/get")
    async def get_value(request: Request):
        return {"who": request.session.get("who")}


def test_secret_key_property_binds_config():
    app = Veloce(openapi_url=None)
    app.secret_key = "k" * 32
    assert app.config["SECRET_KEY"] == "k" * 32
    app.config["SECRET_KEY"] = "m" * 32
    assert app.secret_key == "m" * 32


def test_secret_key_from_app_config_signs_sessions():
    """`SessionMiddleware()` without `secret_key=` signs with `app.secret_key`."""
    app = Veloce(openapi_url=None)
    app.secret_key = "k" * 32
    app.add_middleware(SessionMiddleware)
    _add_session_routes(app)

    with TestClient(app) as client:
        resp = client.get("/set")
        assert resp.headers.get("set-cookie", "").startswith("session=")
        assert client.get("/get").json() == {"who": "alice"}


def test_missing_secret_key_raises_on_first_request():
    app = Veloce(openapi_url=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True
    app.add_middleware(SessionMiddleware)
    _add_session_routes(app)

    with TestClient(app) as client, pytest.raises(RuntimeError, match="secret key"):
        client.get("/set")


def test_session_cookie_name_from_config():
    app = Veloce(openapi_url=None)
    app.secret_key = "k" * 32
    app.config["SESSION_COOKIE_NAME"] = "sid"
    app.add_middleware(SessionMiddleware)
    _add_session_routes(app)

    with TestClient(app) as client:
        resp = client.get("/set")
        assert resp.headers.get("set-cookie", "").startswith("sid=")
        # The configured name round-trips on the read side too.
        assert client.get("/get").json() == {"who": "alice"}


def test_explicit_constructor_args_beat_config():
    app = Veloce(openapi_url=None)
    app.secret_key = "k" * 32
    app.config["SESSION_COOKIE_NAME"] = "sid"
    app.add_middleware(SessionMiddleware, cookie_name="explicit")
    _add_session_routes(app)

    with TestClient(app) as client:
        resp = client.get("/set")
        assert resp.headers.get("set-cookie", "").startswith("explicit=")


def test_session_cookie_flags_from_config():
    app = Veloce(openapi_url=None)
    app.secret_key = "k" * 32
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "strict"
    app.add_middleware(SessionMiddleware)
    _add_session_routes(app)

    with TestClient(app) as client:
        cookie = client.get("/set").headers.get("set-cookie", "")
    assert "Secure" in cookie
    assert "SameSite=Strict" in cookie


def test_use_secure_defaults_flows_into_session_cookie():
    """`use_secure_defaults()` writes the SESSION_COOKIE_* keys; a config-backed
    session middleware actually honours them."""
    app = Veloce(openapi_url=None)
    app.secret_key = "k" * 32
    app.use_secure_defaults()
    app.add_middleware(SessionMiddleware)
    _add_session_routes(app)

    with TestClient(app) as client:
        cookie = client.get("/set").headers.get("set-cookie", "")
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_application_root_sets_cookie_path():
    app = Veloce(openapi_url=None)
    app.secret_key = "k" * 32
    app.config["APPLICATION_ROOT"] = "/app"
    app.add_middleware(SessionMiddleware)
    _add_session_routes(app)

    with TestClient(app) as client:
        cookie = client.get("/set").headers.get("set-cookie", "")
    assert "Path=/app" in cookie


def test_permanent_lifetime_from_config():
    app = Veloce(openapi_url=None)
    app.secret_key = "k" * 32
    app.config["PERMANENT_SESSION_LIFETIME"] = 3600
    app.add_middleware(SessionMiddleware)

    @app.get("/x")
    async def x(request: Request):
        request.session.permanent = True
        request.session["user"] = "alice"
        return {}

    with TestClient(app) as client:
        cookie = client.get("/x").headers.get("set-cookie", "")
    assert "Max-Age=3600" in cookie


def test_server_session_cookie_name_from_config():
    app = Veloce(openapi_url=None)
    app.config["SESSION_COOKIE_NAME"] = "srv"
    app.add_middleware(ServerSessionMiddleware)
    _add_session_routes(app)

    with TestClient(app) as client:
        resp = client.get("/set")
        assert resp.headers.get("set-cookie", "").startswith("srv=")
        assert client.get("/get").json() == {"who": "alice"}
