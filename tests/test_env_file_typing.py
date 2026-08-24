"""An env file carries no types, so its values get the type their key is read as.

Two failures came from the same gap. A numeric key reached a `>` as a string and
raised `TypeError` on every request with a body - a hard 500 through a
documented deployment path. A boolean key was worse than that: a non-empty
string is truthy, so `DEBUG=false` and `JSON_SORT_KEYS=false` turned the setting
*on*, which is the failure mode that looks like the config system is lying.

The type comes from the key's own default, which already describes what the key
holds. `test_every_default_key_has_a_decided_env_type` is what keeps that honest
for keys added later.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.config import _ENV_FREE_FORM, _ENV_TYPED_NONE_DEFAULTS, Config
from veloce.testclient import TestClient


def _app_with_env(tmp_path, body: str) -> Veloce:
    env = tmp_path / ".env"
    env.write_text(body, encoding="utf-8")
    app = Veloce(openapi_url=None)
    app.config.from_env_file(str(env))
    return app


# ── the numeric failure ──────────────────────────────────────────────


def test_a_body_limit_from_an_env_file_is_enforced_not_crashed(tmp_path):
    """The defect: this raised TypeError on every request carrying a body."""
    app = _app_with_env(tmp_path, "MAX_CONTENT_LENGTH=1000\n")

    @app.post("/p")
    async def p(request):
        return {"size": len(await request.body())}

    client = TestClient(app)
    assert client.post("/p", content=b"x" * 10).status_code == 200
    assert client.post("/p", content=b"x" * 2000).status_code == 413


def test_a_numeric_key_is_stored_as_a_number(tmp_path):
    app = _app_with_env(tmp_path, "MAX_CONTENT_LENGTH=1000\nMAX_FORM_PARTS=7\n")
    assert app.config["MAX_CONTENT_LENGTH"] == 1000
    assert app.config["MAX_FORM_PARTS"] == 7


def test_a_none_defaulted_numeric_key_is_still_typed(tmp_path):
    """Its type cannot come from the default, so it is declared instead."""
    app = _app_with_env(tmp_path, "WEBSOCKET_IDLE_TIMEOUT=30\nMAX_FORM_FILE_SIZE=2048\n")
    assert app.config["WEBSOCKET_IDLE_TIMEOUT"] == 30
    assert app.config["MAX_FORM_FILE_SIZE"] == 2048


def test_an_unparseable_number_raises_naming_the_key(tmp_path):
    """Better at load than as a TypeError on the first request with a body."""
    env = tmp_path / ".env"
    env.write_text("MAX_CONTENT_LENGTH=lots\n", encoding="utf-8")
    app = Veloce(openapi_url=None)
    with pytest.raises(ValueError, match="MAX_CONTENT_LENGTH"):
        app.config.from_env_file(str(env))


@pytest.mark.parametrize("raw", ["0", "-1", " 42 "])
def test_numeric_edge_values_are_accepted(tmp_path, raw):
    app = _app_with_env(tmp_path, f"MAX_FORM_PARTS={raw}\n")
    assert app.config["MAX_FORM_PARTS"] == int(raw)


# ── the boolean failure ──────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["false", "False", "FALSE", "0", "no", "off", ""])
def test_a_falsey_string_reads_as_off(tmp_path, raw):
    """The defect: every one of these read as True."""
    app = _app_with_env(tmp_path, f"DEBUG={raw}\nJSON_SORT_KEYS={raw}\nTCP_KEEPALIVE={raw}\n")
    assert app.debug is False
    assert app.config["JSON_SORT_KEYS"] is False
    assert app.config["TCP_KEEPALIVE"] is False


@pytest.mark.parametrize("raw", ["true", "True", "1", "yes", "on"])
def test_a_truthy_string_reads_as_on(tmp_path, raw):
    app = _app_with_env(tmp_path, f"JSON_SORT_KEYS={raw}\n")
    assert app.config["JSON_SORT_KEYS"] is True


def test_json_sort_keys_off_in_an_env_file_does_not_sort(tmp_path):
    """End to end: the setting reaches the wire, not just the config dict."""
    app = _app_with_env(tmp_path, "JSON_SORT_KEYS=false\n")

    @app.get("/j")
    async def j():
        return {"b": 1, "a": 2}

    assert TestClient(app).get("/j").text == '{"b":1,"a":2}'


def test_json_sort_keys_on_in_an_env_file_sorts(tmp_path):
    app = _app_with_env(tmp_path, "JSON_SORT_KEYS=true\n")

    @app.get("/j")
    async def j():
        return {"b": 1, "a": 2}

    assert TestClient(app).get("/j").text == '{"a":2,"b":1}'


def test_a_none_defaulted_boolean_key_is_typed(tmp_path):
    app = _app_with_env(tmp_path, "PROPAGATE_EXCEPTIONS=false\n")
    assert app.config["PROPAGATE_EXCEPTIONS"] is False


# ── list-valued keys ─────────────────────────────────────────────────


def test_a_list_key_splits_on_commas(tmp_path):
    """Left a string, a membership test matches single characters."""
    app = _app_with_env(tmp_path, "SILENCED_AUDIT_IDS=debug-enabled,secret-key-missing\n")
    assert app.config["SILENCED_AUDIT_IDS"] == ("debug-enabled", "secret-key-missing")


def test_a_silenced_id_from_an_env_file_actually_silences(tmp_path):
    from veloce.audit import run

    app = _app_with_env(tmp_path, "SILENCED_AUDIT_IDS=hardening-headers-missing\n")
    app.config["SECRET_KEY"] = "k"
    assert [f.id for f in run(app)] == []


def test_a_single_entry_list_is_still_a_tuple(tmp_path):
    app = _app_with_env(tmp_path, "SILENCED_AUDIT_IDS=debug-enabled\n")
    assert app.config["SILENCED_AUDIT_IDS"] == ("debug-enabled",)


# ── what must NOT be coerced ─────────────────────────────────────────


def test_a_free_form_key_stays_a_string(tmp_path):
    app = _app_with_env(tmp_path, "SECRET_KEY=0123456789\nSERVER_NAME=example.com\n")
    assert app.config["SECRET_KEY"] == "0123456789"
    assert app.config["SERVER_NAME"] == "example.com"


def test_an_unknown_key_stays_a_string(tmp_path):
    """Nothing describes its type, so nothing may guess one."""
    app = _app_with_env(tmp_path, "MY_APP_SETTING=123\n")
    assert app.config["MY_APP_SETTING"] == "123"


def test_a_value_set_in_code_is_untouched(tmp_path):
    """Coercion belongs to the env path only; code already supplies types."""
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = "explicitly a string"
    assert app.config["MAX_CONTENT_LENGTH"] == "explicitly a string"


# ── the guard that keeps this honest ─────────────────────────────────


def test_every_default_key_has_a_decided_env_type():
    """A new config key cannot be added without deciding what a string becomes.

    Typed from its own default, declared in `_ENV_TYPED_NONE_DEFAULTS`, or
    listed in `_ENV_FREE_FORM`. Anything else is an undecided key, which is how
    `MAX_CONTENT_LENGTH` came to crash.
    """
    undecided = [
        key
        for key, default in Config.default_config().items()
        if default is None and key not in _ENV_TYPED_NONE_DEFAULTS and key not in _ENV_FREE_FORM
    ]
    assert undecided == [], f"config keys with no decided env-file type: {undecided}"


def test_the_declared_tables_name_no_key_that_does_not_exist():
    """A renamed key must not leave a dead entry behind."""
    known = set(Config.default_config())
    assert set(_ENV_TYPED_NONE_DEFAULTS) <= known
    assert known >= _ENV_FREE_FORM
