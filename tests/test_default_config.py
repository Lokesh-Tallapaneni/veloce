"""app.config seeded with the built-in default keys (CF-defaults)."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce
from veloce.config import Config


def test_default_config_keys_present():
    app = Veloce()
    for key in ("DEBUG", "TESTING", "SECRET_KEY", "JSON_SORT_KEYS"):
        assert key in app.config


def test_default_config_values():
    app = Veloce()
    assert app.config["DEBUG"] is False
    assert app.config["TESTING"] is False
    assert app.config["JSON_SORT_KEYS"] is True
    assert app.config["APPLICATION_ROOT"] == "/"
    assert app.config["PREFERRED_URL_SCHEME"] == "http"


def test_config_reads_never_keyerror_on_defaults():
    app = Veloce()
    # Previously a bare read could KeyError; now seeded keys return values.
    assert app.config["MAX_CONTENT_LENGTH"] == 100 * 1024 * 1024
    assert app.config["SESSION_COOKIE_NAME"] == "session"


def test_default_config_staticmethod_returns_dict():
    d = Config.default_config()
    assert isinstance(d, dict)
    assert "DEBUG" in d


def test_user_config_overrides_default():
    app = Veloce()
    app.config["DEBUG"] = True
    assert app.config["DEBUG"] is True


def test_each_app_gets_independent_config():
    a = Veloce()
    b = Veloce()
    a.config["DEBUG"] = True
    assert b.config["DEBUG"] is False


def test_permanent_session_lifetime_default():
    app = Veloce()
    # 31 days in seconds — the default.
    assert app.config["PERMANENT_SESSION_LIFETIME"] == 2678400


class TestAppConfig:
    def test_config_dict(self):
        app = Veloce(openapi_url=None)
        app.config["DATABASE_URL"] = "postgres://localhost/db"
        app.config["DEBUG"] = True
        assert app.config["DATABASE_URL"] == "postgres://localhost/db"

    def test_config_update(self):
        app = Veloce(openapi_url=None)
        app.config.update(
            SECRET_KEY="my-secret",
            MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        )
        assert app.config["SECRET_KEY"] == "my-secret"

    def test_secret_key(self):
        app = Veloce(openapi_url=None)
        app.secret_key = "super-secret"
        assert app.secret_key == "super-secret"


class TestConfigAndExtensions:
    """Test config, secret_key, extensions."""

    @pytest.mark.asyncio
    async def test_config_accessible_from_request(self):
        import orjson

        app = Veloce(openapi_url=None)
        app.config["API_KEY"] = "secret123"

        @app.get("/config")
        async def get_config(request: Request):
            return {"key": request.app.config["API_KEY"]}

        resp = await app.handle_request(make_request(path="/config"))
        assert orjson.loads(resp.body)["key"] == "secret123"

    @pytest.mark.asyncio
    async def test_secret_key_from_request(self):
        import orjson

        app = Veloce(openapi_url=None)
        app.secret_key = "super-secret"

        @app.get("/secret")
        async def get_secret(request: Request):
            return {"has_secret": request.app.secret_key is not None}

        resp = await app.handle_request(make_request(path="/secret"))
        assert orjson.loads(resp.body)["has_secret"] is True
