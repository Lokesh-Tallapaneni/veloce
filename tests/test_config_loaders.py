"""`app.config` loaders (CF1/CF2/CF5/CF4/CF6/CF7)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from veloce import Veloce
from veloce.config import Config

# ── _is_uppercase_key ─────────────────────────────────────────────────


def test_uppercase_key_predicate():
    assert Config._is_uppercase_key("DEBUG")
    assert Config._is_uppercase_key("X_API_KEY")
    assert Config._is_uppercase_key("A1")
    assert not Config._is_uppercase_key("debug")
    assert not Config._is_uppercase_key("_PRIVATE")  # leading underscore
    assert not Config._is_uppercase_key("1ABC")  # leading digit
    assert not Config._is_uppercase_key("")
    assert not Config._is_uppercase_key("Mixed_Case")


# ── from_mapping (CF5) ────────────────────────────────────────────────


def test_from_mapping_uppercase_only():
    cfg = Config()
    cfg.from_mapping({"DEBUG": True, "SECRET_KEY": "x", "lower": "skipped"})
    assert cfg["DEBUG"] is True
    assert cfg["SECRET_KEY"] == "x"
    assert "lower" not in cfg


def test_from_mapping_kwargs_form():
    cfg = Config()
    cfg.from_mapping(DEBUG=False, FOO=1)
    assert cfg["DEBUG"] is False
    assert cfg["FOO"] == 1


def test_from_mapping_returns_true():
    cfg = Config()
    assert cfg.from_mapping(DEBUG=True) is True


# ── from_object (CF1) ─────────────────────────────────────────────────


class _Settings:
    DEBUG = True
    DB_URL = "postgres://x"
    _private = "skipped"
    lowercase = "skipped"


def test_from_object_with_class():
    cfg = Config()
    cfg.from_object(_Settings)
    assert cfg["DEBUG"] is True
    assert cfg["DB_URL"] == "postgres://x"
    assert "_private" not in cfg
    assert "lowercase" not in cfg


def test_from_object_with_instance():
    cfg = Config()
    cfg.from_object(_Settings())
    assert cfg["DEBUG"] is True


def test_from_object_with_module_object():
    """from_object accepts a module object."""
    import veloce  # module attribute access

    cfg = Config()
    cfg.from_object(veloce)
    # veloce exports no UPPERCASE attrs, so this should be a no-op (but
    # not raise). Verify by asserting the loader returned True.
    # The check is that no error was raised + return value is True.
    assert cfg.from_object(veloce) is True


def test_from_object_with_dotted_path():
    """A dotted-path string resolves the class then pulls attrs."""
    cfg = Config()
    cfg.from_object(f"{_Settings.__module__}.{_Settings.__name__}")
    assert cfg["DEBUG"] is True


# ── from_pyfile (CF2) ─────────────────────────────────────────────────


def test_from_pyfile_loads_uppercase(tmp_path: Path):
    cfg_file = tmp_path / "settings.py"
    cfg_file.write_text(
        "DEBUG = True\nSECRET_KEY = 'sekret'\nlowercase_skipped = 'no'\n_private = 'no'\n"
    )
    cfg = Config()
    cfg.from_pyfile(str(cfg_file))
    assert cfg["DEBUG"] is True
    assert cfg["SECRET_KEY"] == "sekret"
    assert "lowercase_skipped" not in cfg
    assert "_private" not in cfg


def test_from_pyfile_missing_raises_by_default(tmp_path: Path):
    cfg = Config()
    with pytest.raises(OSError):
        cfg.from_pyfile(str(tmp_path / "does-not-exist.py"))


def test_from_pyfile_silent_swallows_missing_file(tmp_path: Path):
    cfg = Config()
    assert cfg.from_pyfile(str(tmp_path / "absent.py"), silent=True) is False


# ── from_envvar (CF4) ─────────────────────────────────────────────────


def test_from_envvar_reads_filename_from_env(tmp_path: Path, monkeypatch):
    cfg_file = tmp_path / "envcfg.py"
    cfg_file.write_text("API_KEY = 'from-env'\n")
    monkeypatch.setenv("VELOCE_CFG_TEST", str(cfg_file))
    cfg = Config()
    cfg.from_envvar("VELOCE_CFG_TEST")
    assert cfg["API_KEY"] == "from-env"


def test_from_envvar_unset_raises(monkeypatch):
    monkeypatch.delenv("VELOCE_CFG_MISSING", raising=False)
    cfg = Config()
    with pytest.raises(RuntimeError):
        cfg.from_envvar("VELOCE_CFG_MISSING")


def test_from_envvar_silent_when_unset(monkeypatch):
    monkeypatch.delenv("VELOCE_CFG_MISSING", raising=False)
    cfg = Config()
    assert cfg.from_envvar("VELOCE_CFG_MISSING", silent=True) is False


# ── from_prefixed_env (CF6) ───────────────────────────────────────────


def test_from_prefixed_env_pulls_matching_vars(monkeypatch):
    monkeypatch.setenv("MYAPP_DEBUG", "true")
    monkeypatch.setenv("MYAPP_DB_URL", '"postgres://x"')
    monkeypatch.setenv("UNRELATED", "ignored")
    cfg = Config()
    cfg.from_prefixed_env(prefix="MYAPP")
    # JSON `true` decodes to Python True.
    assert cfg["DEBUG"] is True
    # Quoted JSON string decodes to a Python string.
    assert cfg["DB_URL"] == "postgres://x"
    assert "UNRELATED" not in cfg


def test_from_prefixed_env_json_decodes_values(monkeypatch):
    monkeypatch.setenv("MYAPP_TRUE", "true")
    monkeypatch.setenv("MYAPP_NUM", "42")
    monkeypatch.setenv("MYAPP_RAW", "not-json")
    cfg = Config()
    cfg.from_prefixed_env(prefix="MYAPP")
    assert cfg["TRUE"] is True
    assert cfg["NUM"] == 42
    # JSON decode failure → raw string fallback.
    assert cfg["RAW"] == "not-json"


def test_from_prefixed_env_nested_keys(monkeypatch):
    monkeypatch.setenv("MYAPP_MAIL__SERVER", '"smtp.example.com"')
    monkeypatch.setenv("MYAPP_MAIL__PORT", "587")
    cfg = Config()
    cfg.from_prefixed_env(prefix="MYAPP")
    assert cfg["MAIL"] == {"SERVER": "smtp.example.com", "PORT": 587}


# ── from_file (CF7) ───────────────────────────────────────────────────


def test_from_file_loads_json(tmp_path: Path):
    cfg_file = tmp_path / "settings.json"
    cfg_file.write_text(json.dumps({"DEBUG": True, "API_KEY": "x", "lower": "skip"}))
    cfg = Config()
    cfg.from_file(str(cfg_file), load=json.load)
    assert cfg["DEBUG"] is True
    assert cfg["API_KEY"] == "x"
    # from_mapping skips lowercase keys.
    assert "lower" not in cfg


def test_from_file_silent_on_missing(tmp_path: Path):
    cfg = Config()
    assert cfg.from_file(str(tmp_path / "absent.json"), silent=True) is False


def test_from_file_requires_mapping_return():
    """A loader that returns a non-mapping must be rejected."""
    cfg = Config()
    # Write a JSON list (not a mapping) and try to load it.
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write("[1, 2, 3]")
        path = f.name
    try:
        with pytest.raises(TypeError):
            cfg.from_file(path, load=json.load)
    finally:
        os.unlink(path)


# ── get_namespace ─────────────────────────────────────────────────────


def test_get_namespace_extracts_subset():
    cfg = Config()
    cfg.from_mapping(
        MAIL_SERVER="smtp.example.com",
        MAIL_PORT=587,
        DB_URL="postgres://x",
    )
    mail = cfg.get_namespace("MAIL_")
    assert mail == {"server": "smtp.example.com", "port": 587}


def test_get_namespace_preserves_case_when_requested():
    cfg = Config()
    cfg.from_mapping(MAIL_SERVER="x", MAIL_PORT=1)
    mail = cfg.get_namespace("MAIL_", lowercase=False)
    assert "SERVER" in mail and "PORT" in mail


def test_get_namespace_without_trim():
    cfg = Config()
    cfg.from_mapping(MAIL_SERVER="x")
    mail = cfg.get_namespace("MAIL_", trim_namespace=False, lowercase=False)
    assert "MAIL_SERVER" in mail


# ── Integration: app.config is a Config ───────────────────────────────


def test_app_config_is_a_config_instance():
    app = Veloce(openapi_url=None)
    assert isinstance(app.config, Config)
    # Existing dict idioms still work — Config is a dict subclass.
    app.config["X"] = 1
    assert app.config["X"] == 1
    app.config.update({"Y": 2})
    assert app.config["Y"] == 2


def test_app_config_loaders_chain():
    """Multiple loaders compose on the same Config instance."""
    app = Veloce(openapi_url=None)
    app.config.from_mapping(DEBUG=False)
    app.config.from_object(_Settings)
    assert app.config["DEBUG"] is True  # from_object overrode
    assert app.config["DB_URL"] == "postgres://x"
