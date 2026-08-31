"""The protocol's fallback timeouts are the shipped config defaults.

`HttpProtocol` carries class-level fallbacks for a protocol built without an app
config. Written as literals they are a second copy of a number that already has
a home in `Config.default_config()`, and a tuned default reaches the config
while the fallback keeps the old value - so a protocol built the two different
ways behaves differently for no stated reason.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from veloce.config import Config
from veloce.serving.protocol import HttpProtocol

PROTOCOL = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce" / "serving" / "protocol.py"
)
TIMEOUTS = ("KEEP_ALIVE_TIMEOUT", "REQUEST_TIMEOUT")


@pytest.mark.parametrize("name", TIMEOUTS)
def test_the_fallback_equals_the_shipped_default(name: str) -> None:
    assert getattr(HttpProtocol, name) == Config.default_config()[name]


@pytest.mark.parametrize("name", TIMEOUTS)
def test_the_fallback_is_read_from_the_config_not_restated(name: str) -> None:
    """Equal values would also pass by coincidence; this checks the binding."""
    tree = ast.parse(PROTOCOL.read_text(encoding="utf-8"))
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    ]
    assert len(assignments) == 1, f"expected one class-level {name}, found {len(assignments)}"
    rendered = ast.unparse(assignments[0].value)
    assert "default_config()" in rendered, (
        f"{name} is a literal ({rendered}); read it from Config.default_config() "
        "so the two cannot drift"
    )
    assert not isinstance(assignments[0].value, ast.Constant)


def test_a_config_value_still_overrides_the_fallback() -> None:
    """The fallback is a fallback, not a hardcoded ceiling."""
    config = Config()
    config["REQUEST_TIMEOUT"] = 5
    assert config.get("REQUEST_TIMEOUT", HttpProtocol.REQUEST_TIMEOUT) == 5
