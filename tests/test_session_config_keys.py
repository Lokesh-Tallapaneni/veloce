"""The signing key both session middlewares still take from `app.config`."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce
from veloce.middleware.sessions import SessionMiddleware
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
