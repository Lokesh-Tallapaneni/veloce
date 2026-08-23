"""Optional integrations are not imported by `import veloce`.

`contrib/*` is optional by policy - the style guide asks these modules to keep
their imports lazy and raise an actionable hint when a dependency is missing.
The gateways defeated that at the package level: importing `veloce` reached
`MCPContext`, which initialised the whole MCP subpackage (server, registries,
tasks, both transports), and `veloce.contrib`, which pulled OpenAPI, Redis,
static files and templating. Every application paid for all of it, including
ones that expose no tools and render no templates.

These tests pin the behaviour rather than the saving: a number would date, but
"a fresh interpreter that imports veloce has not imported the MCP server" is
the property that matters and is what regresses silently.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: Modules an application only pays for when it actually uses the integration.
_OPTIONAL = [
    "veloce.contrib.mcp",
    "veloce.contrib.mcp.server",
    "veloce.contrib.openapi",
    "veloce.contrib.redis",
]


def _run(*lines: str) -> str:
    """Run `lines` in a fresh interpreter and return its stdout."""
    result = subprocess.run(
        [sys.executable, "-c", "\n".join(lines)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _imported_after(statement: str) -> set[str]:
    """Return the `sys.modules` keys left behind by `statement`."""
    out = _run("import sys", statement, "print(chr(10).join(sorted(sys.modules)))")
    return set(out.split())


@pytest.mark.parametrize("module", _OPTIONAL)
def test_importing_veloce_does_not_import_an_optional_integration(module):
    assert module not in _imported_after("import veloce")


def test_importing_veloce_still_binds_every_declared_export():
    """Laziness must not make a name in `__all__` unreachable."""
    out = _run(
        "import veloce",
        "missing = [n for n in veloce.__all__ if not hasattr(veloce, n)]",
        "print('MISSING' if missing else 'ALL-BOUND', *missing)",
    )
    assert out.split()[0] == "ALL-BOUND", out


def test_the_lazily_exported_name_still_resolves():
    assert "veloce.contrib.mcp.context" in _imported_after("from veloce import MCPContext")


def test_the_contrib_gateway_still_serves_its_names():
    assert "veloce.contrib.staticfiles" in _imported_after("from veloce.contrib import StaticFiles")


@pytest.mark.parametrize(
    ("statement", "target"),
    [("import veloce", "veloce"), ("import veloce.contrib as gateway", "gateway")],
)
def test_an_unknown_gateway_name_still_raises_attribute_error(statement, target):
    """A lazy gateway must not turn a typo into a silent `None`."""
    out = _run(
        statement,
        "try:",
        f"    {target}.definitely_not_exported",
        "except AttributeError as exc:",
        "    print('raised', exc)",
        "else:",
        "    print('no-error')",
    )
    assert out.startswith("raised"), out
    assert "definitely_not_exported" in out
