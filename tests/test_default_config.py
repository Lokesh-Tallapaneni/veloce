"""app.config seeded with the built-in default keys (CF-defaults)."""

from __future__ import annotations

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
    assert app.config["JSON_SORT_KEYS"] is False
    assert app.config["PREFERRED_URL_SCHEME"] == "http"


def test_config_reads_never_keyerror_on_defaults():
    app = Veloce()
    # Previously a bare read could KeyError; now seeded keys return values.
    assert app.config["MAX_CONTENT_LENGTH"] == 100 * 1024 * 1024
    assert app.config["PREFERRED_URL_SCHEME"] == "http"


def test_the_session_cookie_keys_are_not_seeded():
    """They configure nothing - a session middleware takes its cookie settings
    from its own constructor - so seeding them would advertise a knob that
    does not turn."""
    app = Veloce()
    for key in (
        "SESSION_COOKIE_NAME",
        "SESSION_COOKIE_SECURE",
        "SESSION_COOKIE_HTTPONLY",
        "SESSION_COOKIE_SAMESITE",
        "APPLICATION_ROOT",
        "MAX_COOKIE_SIZE",
        "PERMANENT_SESSION_LIFETIME",
    ):
        assert key not in app.config


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


def test_the_session_lifetime_default_lives_on_the_middleware():
    """It configures the cookie, so it belongs to the middleware, not config."""
    from veloce import SessionMiddleware

    assert "PERMANENT_SESSION_LIFETIME" not in Veloce().config
    # 31 days in seconds - the default.
    assert SessionMiddleware(secret_key="k" * 32).permanent_lifetime == 2678400


def test_config_dict():
    app = Veloce(openapi_url=None)
    app.config["DATABASE_URL"] = "postgres://localhost/db"
    app.config["DEBUG"] = True
    assert app.config["DATABASE_URL"] == "postgres://localhost/db"


def test_config_update():
    app = Veloce(openapi_url=None)
    app.config.update(
        SECRET_KEY="my-secret",
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    )
    assert app.config["SECRET_KEY"] == "my-secret"


def test_secret_key():
    app = Veloce(openapi_url=None)
    app.secret_key = "super-secret"
    assert app.secret_key == "super-secret"


class TestConfigAndExtensions:
    """Test config, secret_key, extensions."""

    async def test_config_accessible_from_request(self):
        import orjson

        app = Veloce(openapi_url=None)
        app.config["API_KEY"] = "secret123"

        @app.get("/config")
        async def get_config(request: Request):
            return {"key": request.app.config["API_KEY"]}

        resp = await app.handle_request(make_request(path="/config"))
        assert orjson.loads(resp.body)["key"] == "secret123"

    async def test_secret_key_from_request(self):
        import orjson

        app = Veloce(openapi_url=None)
        app.secret_key = "super-secret"

        @app.get("/secret")
        async def get_secret(request: Request):
            return {"has_secret": request.app.secret_key is not None}

        resp = await app.handle_request(make_request(path="/secret"))
        assert orjson.loads(resp.body)["has_secret"] is True
