"""`app.config` loaders (CF1/CF2/CF5/CF4/CF6/CF7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import veloce
from veloce import Veloce
from veloce.config import Config

# ── _is_uppercase_key ─────────────────────────────────────────────────


def _config() -> Config:
    """A config carrying the framework defaults, as an app would build it."""
    config = Config()
    config.update(Config.default_config())
    return config


def test_uppercase_key_predicate():
    assert Config._is_uppercase_key("DEBUG")
    assert Config._is_uppercase_key("X_API_KEY")
    assert Config._is_uppercase_key("A1")
    assert not Config._is_uppercase_key("debug")
    assert not Config._is_uppercase_key("_PRIVATE")  # leading underscore
    assert not Config._is_uppercase_key("1ABC")  # leading digit
    assert not Config._is_uppercase_key("")
    assert not Config._is_uppercase_key("Mixed_Case")
    # Non-ASCII "digit" characters must be rejected: the contract is ASCII
    # A-Z/0-9/_, but str.isdigit() also accepts superscripts and other scripts.
    assert not Config._is_uppercase_key("KEY²")  # superscript two
    assert not Config._is_uppercase_key("KEY٠")  # Arabic-Indic zero


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
    """`from_object` accepts a module and takes its UPPERCASE attributes.

    The comment this replaces said the `veloce` module exports none, making the
    call a no-op - it exports two (`TYPE_CHECKING` and `URL`), and the test
    asserted only the return value, so the comment and the code disagreed and
    nothing checked either.
    """
    cfg = Config()
    assert cfg.from_object(veloce) is True

    loaded = {key: value for key, value in cfg.items() if hasattr(veloce, key)}
    assert loaded, "nothing was loaded from the module"
    for key, value in loaded.items():
        assert key.isupper(), f"{key} is not UPPERCASE and was loaded anyway"
        assert value is getattr(veloce, key)
    assert "Veloce" not in cfg, "a non-UPPERCASE attribute was loaded"


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


def test_from_file_requires_mapping_return(tmp_path: Path):
    """A loader that returns a non-mapping must be rejected."""
    cfg = Config()
    # A JSON list, not a mapping.
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]")
    # Matched on the message, not just the type: `from_mapping` raises its own
    # TypeError on a non-mapping further down, so a bare `raises(TypeError)`
    # passes even with this guard removed and pins nothing.
    with pytest.raises(TypeError, match="expected a mapping"):
        cfg.from_file(str(path), load=json.load)


# ── from_env_file ────────────────────────────────────────────────────


def test_from_env_file_loads_key_value_pairs(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "SECRET_KEY=s3cr3t\n"
        "export DATABASE_URL=postgres://localhost/db\n"
        'QUOTED="quoted value"\n'
        "SINGLE='single quoted'\n"
        "lowercase=ignored\n"
    )
    app = Veloce(openapi_url=None)
    loaded = app.config.from_env_file(str(env))

    assert loaded is True
    assert app.config["SECRET_KEY"] == "s3cr3t"
    assert app.config["DATABASE_URL"] == "postgres://localhost/db"
    assert app.config["QUOTED"] == "quoted value"
    assert app.config["SINGLE"] == "single quoted"
    # Only UPPERCASE keys are stored.
    assert "lowercase" not in app.config


def test_from_env_file_missing_file_silent_returns_false(tmp_path):
    app = Veloce(openapi_url=None)
    assert app.config.from_env_file(str(tmp_path / "absent.env"), silent=True) is False


def test_from_env_file_missing_file_raises_without_silent(tmp_path):
    app = Veloce(openapi_url=None)
    with pytest.raises(OSError):
        app.config.from_env_file(str(tmp_path / "absent.env"))


def test_from_env_file_ignores_malformed_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("VALID=ok\nthis line has no equals sign\nANOTHER=fine\n")
    app = Veloce(openapi_url=None)
    app.config.from_env_file(str(env))

    assert app.config["VALID"] == "ok"
    assert app.config["ANOTHER"] == "fine"


def test_from_env_file_strips_an_unquoted_inline_comment(tmp_path):
    """An unquoted value drops a trailing ` #` inline comment instead of
    keeping it as part of the value."""
    env = tmp_path / ".env"
    env.write_text("HOST=localhost  # the dev host\nPORT=8000\n")
    app = Veloce(openapi_url=None)
    app.config.from_env_file(str(env))

    assert app.config["HOST"] == "localhost"
    assert app.config["PORT"] == "8000"


def test_from_env_file_keeps_a_hash_inside_a_quoted_value(tmp_path):
    """A `#` inside quotes is literal; only a comment after the closing
    quote is stripped."""
    env = tmp_path / ".env"
    env.write_text('PASSWORD="p#ss w#rd"  # not part of the value\n')
    app = Veloce(openapi_url=None)
    app.config.from_env_file(str(env))

    assert app.config["PASSWORD"] == "p#ss w#rd"


def test_from_env_file_keeps_a_bare_hash_without_leading_space(tmp_path):
    """Only a whitespace-delimited ` #` starts a comment — a `#` with no
    leading space is part of the value."""
    env = tmp_path / ".env"
    env.write_text("COLOR=#ff0000\n")
    app = Veloce(openapi_url=None)
    app.config.from_env_file(str(env))

    assert app.config["COLOR"] == "#ff0000"


def test_from_env_file_warns_on_unmatched_quote(tmp_path, caplog):
    """An unmatched opening quote is salvaged but emits a warning that
    names the file, the 1-indexed line number, the key, and the quote
    character so a typo does not silently truncate to a bad value."""
    env = tmp_path / ".env"
    env.write_text('FIRST=ok\nDB_URL="postgres://user@host/db\n')
    app = Veloce(openapi_url=None)

    with caplog.at_level("WARNING", logger="veloce.config"):
        app.config.from_env_file(str(env))

    assert app.config["FIRST"] == "ok"
    assert app.config["DB_URL"] == "postgres://user@host/db"

    matching = [r for r in caplog.records if r.name == "veloce.config"]
    assert len(matching) == 1
    msg = matching[0].getMessage()
    assert "line 2" in msg
    assert "'DB_URL'" in msg
    assert str(env) in msg
    assert '"' in msg


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


# ── from_mapping keyword arguments reach the config ──────────────────
#
# `Config.from_mapping(debug=True)` stored nothing and returned `True`, so the
# caller could not tell that `DEBUG` had been left at its default. A `mapping`
# is different: a settings dict legitimately carries entries that are not
# config keys, so those are still filtered quietly.


def test_the_message_shows_the_uppercase_form():
    with pytest.raises(TypeError, match="DEBUG"):
        _config().from_mapping(debug=True)


def test_several_bad_keywords_are_all_named():
    with pytest.raises(TypeError, match="alpha, beta"):
        _config().from_mapping(beta=1, alpha=2)


def test_an_uppercase_keyword_is_stored():
    """The negative: refusing everything would pass the tests above vacuously."""
    config = _config()
    assert config.from_mapping(TESTING=True) is True
    assert config["TESTING"] is True


def test_a_mapping_still_filters_quietly():
    """A settings dict may legitimately carry entries that are not config."""
    config = _config()
    config.from_mapping({"DEBUG": True, "debug": False, "note": "x"})
    assert config["DEBUG"] is True
    assert "note" not in config
    assert "debug" not in config


def test_a_mapping_and_keywords_combine():
    config = _config()
    config.from_mapping({"DEBUG": True}, TESTING=True)
    assert config["DEBUG"] is True
    assert config["TESTING"] is True


def test_a_bad_keyword_is_refused_before_anything_is_stored():
    """A partial application would be worse than either outcome."""
    config = _config()
    with pytest.raises(TypeError):
        config.from_mapping({"DEBUG": True}, testing=True)
    assert config["DEBUG"] is False


def test_no_arguments_is_still_fine():
    assert _config().from_mapping() is True
