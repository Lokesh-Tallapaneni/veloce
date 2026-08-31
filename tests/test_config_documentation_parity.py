"""The configuration guide and the shipped defaults describe the same settings.

Documentation drifts silently. Three keys shipped with defaults and appeared in
no table, so nobody could discover them; four more were read by live code and
existed in neither the defaults nor the docs, which is worse — an undeclared key
has no documented default, no type, and no coercion, and that is how
`MCP_CALL_TIMEOUT=5` from an env file reached `asyncio.wait_for` as a string and
broke every tool call.

These tests are the fix. Adding a key without documenting it, or documenting one
that does not exist, fails here rather than being noticed a release later.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from veloce import Veloce
from veloce.config import _ENV_FREE_FORM, _ENV_TYPED_NONE_DEFAULTS, Config
from veloce.contrib.mcp.server import MCPServer

GUIDE = pathlib.Path(__file__).resolve().parents[1] / "docs" / "guide" / "configuration.md"


def _documented_keys() -> set[str]:
    """Every key named in the guide's configuration table."""
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]*)`", GUIDE.read_text(encoding="utf-8"), re.M))


# ── the two directions ───────────────────────────────────────────────


def test_every_shipped_key_is_documented():
    """The defect: three keys shipped and appeared in no table."""
    undocumented = sorted(set(Config.default_config()) - _documented_keys())
    assert undocumented == [], f"config keys with no row in the guide: {undocumented}"


def test_every_documented_key_is_shipped():
    """A row for a key that does not exist sends the reader to set nothing."""
    phantom = sorted(_documented_keys() - set(Config.default_config()))
    assert phantom == [], f"documented config keys that do not exist: {phantom}"


# ── the keys that were read but never declared ───────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "EVENT_LOOP_WATCHDOG",
        "MCP_CALL_TIMEOUT",
        "MCP_ENFORCE_LIFECYCLE",
        "MCP_RESOURCE_SUBSCRIPTIONS",
        "MAX_CONCURRENT_CONNECTIONS",
        "WRITE_BUFFER_HIGH_WATER",
        "SILENCED_AUDIT_IDS",
    ],
)
def test_a_previously_undeclared_key_now_has_a_default(key):
    assert key in Config.default_config()
    assert key in Veloce(openapi_url=None).config


def test_the_mcp_defaults_preserve_the_previous_behaviour():
    """Declaring a key must not turn a feature on for anyone."""
    defaults = Config.default_config()
    assert defaults["MCP_CALL_TIMEOUT"] is None
    assert defaults["MCP_ENFORCE_LIFECYCLE"] is False
    assert defaults["MCP_RESOURCE_SUBSCRIPTIONS"] is False
    assert defaults["EVENT_LOOP_WATCHDOG"] is None


# ── declaring them gave them env-file typing ─────────────────────────


def _from_env(tmp_path, body: str) -> Veloce:
    env = tmp_path / ".env"
    env.write_text(body, encoding="utf-8")
    app = Veloce(openapi_url=None)
    app.config.from_env_file(str(env))
    return app


def test_the_mcp_call_timeout_is_a_number_from_an_env_file(tmp_path):
    """The defect: it reached `asyncio.wait_for` as the string '5'."""
    assert _from_env(tmp_path, "MCP_CALL_TIMEOUT=5\n").config["MCP_CALL_TIMEOUT"] == 5


@pytest.mark.parametrize("key", ["MCP_ENFORCE_LIFECYCLE", "MCP_RESOURCE_SUBSCRIPTIONS"])
def test_an_mcp_flag_reads_as_off_from_an_env_file(tmp_path, key):
    """The defect: `false` is a non-empty string, so both read as on."""
    assert _from_env(tmp_path, f"{key}=false\n").config[key] is False


def test_the_watchdog_flag_reads_as_off_from_an_env_file(tmp_path):
    assert _from_env(tmp_path, "EVENT_LOOP_WATCHDOG=false\n").config["EVENT_LOOP_WATCHDOG"] is False


def test_a_watchdog_mapping_set_in_code_is_untouched():
    """Coercion applies to env strings only; a mapping still tunes it."""
    app = Veloce(openapi_url=None)
    app.config["EVENT_LOOP_WATCHDOG"] = {"interval": 0.5}
    assert app.config["EVENT_LOOP_WATCHDOG"] == {"interval": 0.5}


def test_the_mcp_server_reads_the_typed_values(tmp_path):
    """End to end: the declared default reaches the component that uses it."""

    app = _from_env(tmp_path, "MCP_ENFORCE_LIFECYCLE=false\nMCP_RESOURCE_SUBSCRIPTIONS=false\n")
    server = MCPServer(app)
    assert server._enforce_lifecycle is False
    assert server._subscriptions_enabled is False


# ── the typing tables stay in step with the defaults ─────────────────


def test_every_none_defaulted_key_has_a_decided_type():
    """Restated here because declaring a new key is where this gets forgotten."""
    undecided = [
        key
        for key, default in Config.default_config().items()
        if default is None and key not in _ENV_TYPED_NONE_DEFAULTS and key not in _ENV_FREE_FORM
    ]
    assert undecided == [], f"keys with no decided env-file type: {undecided}"


def test_the_typing_tables_name_no_key_that_does_not_exist():
    known = set(Config.default_config())
    assert set(_ENV_TYPED_NONE_DEFAULTS) <= known
    assert known >= _ENV_FREE_FORM
