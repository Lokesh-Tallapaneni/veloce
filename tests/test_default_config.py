"""app.config seeded with the built-in default keys (CF-defaults)."""

from __future__ import annotations

from veloce import Veloce
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
    assert app.config["MAX_CONTENT_LENGTH"] is None
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
