"""Both env loaders coerce a value to its declared type.

`from_prefixed_env` gave a value its declared type and `from_env` did not, so
the same variable meant different things depending on which loader read it.

Split out of `test_entry_point_parity.py`, which was named for the change that
produced it and held three unrelated subsystems: someone changing an env loader
had no name- or `-k`-discoverable file to look in.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.config import Config
from veloce.testclient import TestClient


def _config() -> Config:
    """A config carrying the framework defaults, as an app would build it."""
    config = Config()
    config.update(Config.default_config())
    return config


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("MAX_CONTENT_LENGTH", "1000", 1000),
        ("DEBUG", "False", False),
        ("DEBUG", "true", True),
        ("MCP_CALL_TIMEOUT", "30", 30),
    ],
)
def test_from_prefixed_env_gives_a_value_its_declared_type(monkeypatch, key, raw, expected):
    monkeypatch.setenv(f"VELOCE_{key}", raw)
    config = _config()
    config.from_prefixed_env()
    assert config[key] == expected
    assert type(config[key]) is type(expected)


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("MAX_CONTENT_LENGTH", "lots"),
        ("MAX_CONTENT_LENGTH", "10MB"),
        ("MCP_CALL_TIMEOUT", "5s"),
        ("DEBUG", "banana"),
    ],
)
def test_from_prefixed_env_refuses_a_value_of_the_wrong_type(monkeypatch, key, raw):
    """The defect: these were stored as strings and failed per request instead."""
    monkeypatch.setenv(f"VELOCE_{key}", raw)
    config = _config()
    with pytest.raises(ValueError, match=key):
        config.from_prefixed_env()


@pytest.mark.parametrize(
    ("key", "raw"),
    [("MAX_CONTENT_LENGTH", "lots"), ("MCP_CALL_TIMEOUT", "5s"), ("DEBUG", "banana")],
)
def test_the_two_env_loaders_agree(tmp_path, monkeypatch, key, raw):
    """The property: same setting, same value, same outcome."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}={raw}\n", encoding="utf-8")
    from_file = pytest.raises(ValueError)
    with from_file:
        _config().from_env_file(str(env_file))

    monkeypatch.setenv(f"VELOCE_{key}", raw)
    with pytest.raises(ValueError):
        _config().from_prefixed_env()


def test_a_json_value_is_still_decoded(monkeypatch):
    """The existing behaviour: valid JSON wins and is not re-parsed as a string."""
    monkeypatch.setenv("VELOCE_SOME_LIST", "[1, 2, 3]")
    config = _config()
    config.from_prefixed_env()
    assert config["SOME_LIST"] == [1, 2, 3]


def test_an_unknown_key_is_still_stored_as_read(monkeypatch):
    """No declared type means nothing to coerce to."""
    monkeypatch.setenv("VELOCE_CUSTOM_THING", "hello world")
    config = _config()
    config.from_prefixed_env()
    assert config["CUSTOM_THING"] == "hello world"


def test_a_nested_key_is_unchanged(monkeypatch):
    monkeypatch.setenv("VELOCE_MAIL__SERVER", "smtp.example.com")
    config = _config()
    config.from_prefixed_env()
    assert config["MAIL"] == {"SERVER": "smtp.example.com"}


def test_a_body_request_works_after_loading_the_limit(monkeypatch):
    """End to end: the failure this prevents was a 500 on every body request."""
    monkeypatch.setenv("VELOCE_MAX_CONTENT_LENGTH", "1000000")
    app = Veloce(openapi_url=None)
    app.config.from_prefixed_env()

    @app.post("/x")
    async def x(request) -> dict:
        return {"n": len(await request.body())}

    assert TestClient(app).post("/x", json={"a": 1}).json()["n"] > 0
