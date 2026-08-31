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


# ── The version is read only when it is asked for ────────────────────
#
# Reading it walks the installed distribution's metadata files, which a cold
# interpreter pays for in real time - on every CLI invocation and every
# serverless cold start, to produce a string almost nothing asks for.


def test_importing_veloce_does_not_read_the_package_metadata():
    assert "importlib.metadata" not in _imported_after("import veloce")


def test_the_version_still_resolves_on_access():
    out = _run(
        "import veloce",
        "from importlib.metadata import version",
        "print(veloce.__version__ == version('veloceframework'))",
    )
    assert out.strip() == "True"


def test_the_version_is_importable_by_name():
    """`from veloce import __version__` goes through the module `__getattr__`."""
    out = _run("from veloce import __version__", "print(bool(__version__))")
    assert out.strip() == "True"


def test_reading_the_version_caches_it():
    """The metadata walk happens once, not on every access."""
    out = _run(
        "import veloce",
        "print('__version__' in vars(veloce))",
        "veloce.__version__",
        "print('__version__' in vars(veloce))",
    )
    assert out.split() == ["False", "True"]


def test_the_cli_and_the_package_report_the_same_version():
    """Two spellings, one resolver - their fallbacks had drifted apart."""
    out = _run(
        "import veloce",
        "from veloce.cli import _resolve_version",
        "print(_resolve_version() == veloce.__version__)",
    )
    assert out.strip() == "True"


def test_an_unreadable_metadata_store_falls_back_rather_than_failing():
    """A version string is never worth failing an import over."""
    out = _run(
        "import importlib.metadata as md",
        "md.version = lambda name: (_ for _ in ()).throw(md.PackageNotFoundError(name))",
        "from veloce._version import UNKNOWN_VERSION, resolve_version",
        "print(resolve_version() == UNKNOWN_VERSION)",
    )
    assert out.strip() == "True"
