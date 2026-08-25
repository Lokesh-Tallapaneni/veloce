"""A typed config value from a `.env` file is parsed, or refused by name.

Env files carry no types, so every value arrives as a string. Coercion already
existed, but only half of it could fail. `_coerce_int` rejected a non-integer;
the boolean path could not reject anything — every token outside the truthy set
read as `False`:

    DEBUG=false   -> False
    DEBUG=flase   -> False     <- a typo, indistinguishable from the line above
    DEBUG=maybe   -> False
    DEBUG=2       -> False

So a typo in a boolean setting silently selected a value, and for a security
flag that value is the unsafe one. Nothing was logged and nothing raised; the
app simply ran with a setting the operator did not write.

Both types now go through a `pydantic.TypeAdapter`. Pydantic is already a hard
dependency and already in this module's import graph, env-file parsing runs once
at startup, and its token set is the maintained one — so it parses, rather than a
hand-written membership test. It is byte-identical to `int()` on every integer
form tested, and strictly stricter on booleans, which is the whole point.

A value set in *code* is untouched: `app.config["DEBUG"] = 1` still works, and
the lenient `_coerce_bool` still backs the read paths. Only what enters from a
file is validated, which is where the ambiguity is.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.config import _coerce_env_typed, _coerce_env_value


def _from_env(tmp_path, body: str) -> Veloce:
    env = tmp_path / ".env"
    env.write_text(body, encoding="utf-8")
    app = Veloce(openapi_url=None)
    app.config.from_env_file(str(env))
    return app


# ── booleans: what is accepted ───────────────────────────────────────


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("true", True),
        ("false", False),
        ("True", True),
        ("FALSE", False),
        ("TrUe", True),
        ("yes", True),
        ("no", False),
        ("on", True),
        ("off", False),
        ("1", True),
        ("0", False),
        ("  true  ", True),
        ("\tfalse\t", False),
        # Pydantic also takes the single-letter forms.
        ("y", True),
        ("n", False),
        ("t", True),
        ("f", False),
    ],
)
def test_a_boolean_token_is_parsed(tmp_path, written, expected):
    assert _from_env(tmp_path, f"DEBUG={written}\n").config["DEBUG"] is expected


def test_a_declared_boolean_with_no_default_is_parsed(tmp_path):
    """`MCP_ENFORCE_LIFECYCLE` defaults to `False`, not to a typed `None`."""
    app = _from_env(tmp_path, "MCP_ENFORCE_LIFECYCLE=off\n")
    assert app.config["MCP_ENFORCE_LIFECYCLE"] is False


def test_a_none_defaulted_boolean_is_parsed(tmp_path):
    """`EVENT_LOOP_WATCHDOG` defaults to `None`; its type comes from the table."""
    assert _from_env(tmp_path, "EVENT_LOOP_WATCHDOG=true\n").config["EVENT_LOOP_WATCHDOG"] is True


# ── booleans: what is refused ────────────────────────────────────────


@pytest.mark.parametrize(
    "written",
    ["flase", "ture", "maybe", "2", "-1", "null", "None", "enabled", "sure", "0.0", "off!"],
)
def test_a_value_that_is_not_a_boolean_is_refused(tmp_path, written):
    """The defect: every one of these read as `False` without a word."""
    with pytest.raises(ValueError, match="DEBUG must be a boolean"):
        _from_env(tmp_path, f"DEBUG={written}\n")


def test_the_refusal_names_the_key(tmp_path):
    """So the message points at the line of the file to fix."""
    with pytest.raises(ValueError, match="MCP_ENFORCE_LIFECYCLE"):
        _from_env(tmp_path, "MCP_ENFORCE_LIFECYCLE=flase\n")


def test_the_refusal_shows_the_value_written(tmp_path):
    with pytest.raises(ValueError, match="'flase'"):
        _from_env(tmp_path, "DEBUG=flase\n")


def test_the_refusal_lists_what_is_accepted(tmp_path):
    with pytest.raises(ValueError, match="true/false"):
        _from_env(tmp_path, "DEBUG=flase\n")


def test_an_empty_boolean_reads_as_off(tmp_path):
    """Every dotenv reader treats `KEY=` as empty, and for a flag that is off.

    Kept deliberately: it is a value the operator can have meant. `flase` is
    not, which is the distinction this parser draws.
    """
    assert _from_env(tmp_path, "DEBUG=\n").config["DEBUG"] is False
    assert _from_env(tmp_path, "DEBUG=   \n").config["DEBUG"] is False


def test_a_typo_no_longer_looks_like_the_safe_value(tmp_path):
    """The security shape of the defect, stated directly."""
    assert _from_env(tmp_path, "PROPAGATE_EXCEPTIONS=true\n").config["PROPAGATE_EXCEPTIONS"] is True
    with pytest.raises(ValueError):
        _from_env(tmp_path, "PROPAGATE_EXCEPTIONS=treu\n")


# ── integers: what is accepted ───────────────────────────────────────


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("1000", 1000),
        ("0", 0),
        ("-5", -5),
        ("  2048  ", 2048),
        ("1_000", 1000),
        ("+7", 7),
    ],
)
def test_an_integer_is_parsed(tmp_path, written, expected):
    assert _from_env(tmp_path, f"MAX_CONTENT_LENGTH={written}\n").config[
        "MAX_CONTENT_LENGTH"
    ] == int(expected)


def test_a_none_defaulted_integer_is_parsed(tmp_path):
    """The `MCP_CALL_TIMEOUT=5` case: it reached `asyncio.wait_for` as a string."""
    assert _from_env(tmp_path, "MCP_CALL_TIMEOUT=5\n").config["MCP_CALL_TIMEOUT"] == 5


# ── integers: what is refused ────────────────────────────────────────


@pytest.mark.parametrize("written", ["abc", "", "   ", "10.5", "1e3", "0x10", "5s", "one", "None"])
def test_a_value_that_is_not_an_integer_is_refused(tmp_path, written):
    with pytest.raises(ValueError, match="MAX_CONTENT_LENGTH must be an integer"):
        _from_env(tmp_path, f"MAX_CONTENT_LENGTH={written}\n")


def test_the_integer_refusal_names_the_key_and_value(tmp_path):
    with pytest.raises(ValueError, match="MCP_CALL_TIMEOUT must be an integer, got 'soon'"):
        _from_env(tmp_path, "MCP_CALL_TIMEOUT=soon\n")


def test_a_float_is_refused_rather_than_truncated(tmp_path):
    """Silently dropping the fraction would be its own surprise."""
    with pytest.raises(ValueError):
        _from_env(tmp_path, "MAX_CONTENT_LENGTH=10.9\n")


# ── the untyped and list-valued keys are unchanged ───────────────────


@pytest.mark.parametrize("key", ["SECRET_KEY", "SERVER_NAME", "PREFERRED_URL_SCHEME"])
def test_a_free_form_key_takes_any_text(tmp_path, key):
    assert _from_env(tmp_path, f"{key}=anything at all\n").config[key] == "anything at all"


def test_a_free_form_key_accepts_an_empty_value(tmp_path):
    """Only a *typed* key refuses an empty value."""
    assert _from_env(tmp_path, "SERVER_NAME=\n").config["SERVER_NAME"] == ""


def test_a_tuple_valued_key_is_split_on_commas(tmp_path):
    """`SILENCED_AUDIT_IDS` defaults to `()`, so its type is read off the default."""
    app = _from_env(tmp_path, "SILENCED_AUDIT_IDS=csp-not-sent, routes-undocumented\n")
    assert app.config["SILENCED_AUDIT_IDS"] == ("csp-not-sent", "routes-undocumented")


def test_a_tuple_valued_key_drops_empty_entries(tmp_path):
    app = _from_env(tmp_path, "SILENCED_AUDIT_IDS=a,,b,\n")
    assert app.config["SILENCED_AUDIT_IDS"] == ("a", "b")


def test_a_tuple_valued_key_accepts_a_single_entry(tmp_path):
    """Left a string, a membership test would match single characters."""
    app = _from_env(tmp_path, "SILENCED_AUDIT_IDS=csp-not-sent\n")
    assert app.config["SILENCED_AUDIT_IDS"] == ("csp-not-sent",)


def test_an_unknown_key_is_stored_as_text(tmp_path):
    """Config is an open dict; a user's own key has no declared type."""
    assert _from_env(tmp_path, "MY_TOKEN=abc123\n").config["MY_TOKEN"] == "abc123"


# ── a value set in code is untouched ─────────────────────────────────


@pytest.mark.parametrize("value", [1, 0, True, False, None, "yes", object()])
def test_setting_a_value_in_code_is_not_validated(value):
    """Only what enters from a file is parsed; code says what it means."""
    app = Veloce(openapi_url=None)
    app.config["DEBUG"] = value
    assert app.config["DEBUG"] is value


def test_the_debug_property_still_reads_leniently():
    """`app.debug` coerces on read, so a truthy value set in code still works."""
    app = Veloce(openapi_url=None)
    app.config["DEBUG"] = 1
    assert app.debug is True


def test_a_watchdog_mapping_set_in_code_survives():
    """Declared `bool` for env files; a mapping in code tunes it."""
    app = Veloce(openapi_url=None)
    app.config["EVENT_LOOP_WATCHDOG"] = {"interval": 0.5}
    assert app.config["EVENT_LOOP_WATCHDOG"] == {"interval": 0.5}


def test_a_non_string_reaching_the_coercer_passes_through():
    assert _coerce_env_value("DEBUG", True, False) is True
    assert _coerce_env_value("MAX_CONTENT_LENGTH", 5, 0) == 5


# ── the parser itself ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "written", "expected"),
    [("bool", "true", True), ("bool", "off", False), ("int", "42", 42), ("int", "-1", -1)],
)
def test_the_parser_accepts_a_valid_value(kind, written, expected):
    assert _coerce_env_typed(written, kind, name="K") == expected


@pytest.mark.parametrize(("kind", "written"), [("bool", "nope"), ("int", "nope"), ("int", "")])
def test_the_parser_raises_a_value_error(kind, written):
    """A `ValidationError` would leak pydantic into the caller's exception surface."""
    with pytest.raises(ValueError) as caught:
        _coerce_env_typed(written, kind, name="K")
    assert "K must be" in str(caught.value)
    assert type(caught.value) is ValueError


def test_the_integer_parser_chains_the_underlying_error():
    """So the cause is still there for anyone who wants it."""
    with pytest.raises(ValueError) as caught:
        _coerce_env_typed("nope", "int", name="K")
    assert isinstance(caught.value.__cause__, ValueError)


def test_the_boolean_parser_has_no_cause_to_chain():
    """It is a membership test, so nothing underneath it raised."""
    with pytest.raises(ValueError) as caught:
        _coerce_env_typed("nope", "bool", name="K")
    assert caught.value.__cause__ is None


# ── a whole file, end to end ─────────────────────────────────────────


def test_a_realistic_file_loads(tmp_path):
    app = _from_env(
        tmp_path,
        "DEBUG=false\n"
        "SECRET_KEY=s3cret\n"
        "MAX_CONTENT_LENGTH=1048576\n"
        "MCP_CALL_TIMEOUT=30\n"
        "MCP_ENFORCE_LIFECYCLE=yes\n"
        "SERVER_NAME=api.example.com\n",
    )
    assert app.config["DEBUG"] is False
    assert app.config["SECRET_KEY"] == "s3cret"
    assert app.config["MAX_CONTENT_LENGTH"] == 1048576
    assert app.config["MCP_CALL_TIMEOUT"] == 30
    assert app.config["MCP_ENFORCE_LIFECYCLE"] is True
    assert app.config["SERVER_NAME"] == "api.example.com"


def test_one_bad_line_refuses_the_whole_file(tmp_path):
    """Half-loading a config is worse than not loading it."""
    with pytest.raises(ValueError, match="DEBUG"):
        _from_env(tmp_path, "SECRET_KEY=s3cret\nDEBUG=flase\nMAX_CONTENT_LENGTH=100\n")


def test_the_typed_value_reaches_the_component_that_uses_it(tmp_path):
    """End to end: a parsed int is what `asyncio.wait_for` would be handed."""
    from veloce.contrib.mcp.server import MCPServer

    app = _from_env(tmp_path, "MCP_CALL_TIMEOUT=30\n")
    assert MCPServer(app)._call_timeout == 30
