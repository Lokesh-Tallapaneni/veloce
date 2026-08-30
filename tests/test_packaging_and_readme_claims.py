"""What the README, the example app and the dependency floors promise.

Three findings from the comprehensive audit, and the guards that keep them
fixed. Each is a claim made outside the source tree, where nothing was checking
it against the source.

**The example app used a deprecated API.** `example_app.py` is the first thing a
reader runs, and it called `@app.on_event("startup")` / `("shutdown")`, which
have raised `VeloceDeprecationWarning` since v0.17.0 and go away in v1.0.0. It
now uses `@app.on_startup` / `@app.on_shutdown`, and the test below imports it
with warnings promoted to errors, so the next deprecation to reach it fails here
rather than in a reader's terminal.

**The README feature table omitted whole subsystems.** Server-sent events,
signals, password hashing, caching, rate limiting and the entire MCP surface -
a contrib package with its own guide - were absent from the one table that
tells a visitor what the framework does.

**The dependency floors were far below the versions anyone should install.**
`orjson>=3.9.0`, `pydantic>=2.0.0`, `python-multipart>=0.0.6`, `jinja2>=3.1` and
`gunicorn>=21.0`. The lock file has always resolved to current releases, so this
never affected a contributor - it affected a consumer installing at the floor,
which is exactly who a floor is for. They are raised to the first release
carrying the relevant security fixes.

The floors are asserted as a lower bound rather than an equality, so raising one
again does not fail here.
"""

from __future__ import annotations

import importlib.metadata as metadata
import importlib.util
import pathlib
import re
import warnings

import pytest
import tomllib

import veloce
from veloce import Veloce
from veloce._warnings import VeloceDeprecationWarning
from veloce.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
EXAMPLE = ROOT / "example_app.py"
PYPROJECT = ROOT / "pyproject.toml"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _floor(spec: str) -> tuple[int, ...]:
    """The `>=` floor in a requirement string, as a comparable tuple."""
    match = re.search(r">=\s*([0-9][0-9.]*)", spec)
    assert match is not None, f"no floor in {spec!r}"
    return tuple(int(part) for part in match.group(1).split("."))


def _requirement(name: str) -> str:
    data = _pyproject()
    for spec in data["project"]["dependencies"]:
        if spec.split(">=")[0].split(";")[0].strip() == name:
            return spec
    for specs in data["project"]["optional-dependencies"].values():
        for spec in specs:
            if spec.split(">=")[0].split(";")[0].strip() == name:
                return spec
    raise AssertionError(f"{name} is not a declared dependency")


# ── the example app runs on current API ──────────────────────────────


def test_the_example_app_uses_no_deprecated_lifecycle_hook():
    """The defect: the first file a reader runs called a deprecated API."""
    assert "on_event(" not in EXAMPLE.read_text(encoding="utf-8")


def test_the_example_app_imports_without_a_deprecation_warning():
    """The guard that matters - a future deprecation fails here, not silently."""
    spec = importlib.util.spec_from_file_location("_example_app_probe", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with warnings.catch_warnings():
        warnings.simplefilter("error", category=UserWarning)
        spec.loader.exec_module(module)


def test_the_example_app_still_serves():
    """End to end: the migrated hooks must actually run."""
    spec = importlib.util.spec_from_file_location("_example_app_serve", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with TestClient(module.app) as client:
        assert client.get("/").status_code == 200


def test_on_event_is_still_deprecated():
    """The negative: if it stopped warning, the test above would prove nothing."""

    app = Veloce(openapi_url=None)
    with pytest.warns(VeloceDeprecationWarning, match="on_event"):

        @app.on_event("startup")
        async def _startup() -> None:
            pass


# ── the README describes what is actually shipped ────────────────────


@pytest.mark.parametrize(
    ("area", "needle"),
    [
        ("server-sent events", "EventSourceResponse"),
        ("signals", "request_finished"),
        ("password hashing", "hash_password"),
        ("caching", "@cached"),
        ("rate limiting", "TokenBucket"),
        ("mcp", "Model Context Protocol"),
        ("scaffolding", "veloce new"),
        ("compression", "brotli"),
    ],
)
def test_the_feature_table_mentions_each_shipped_area(area: str, needle: str):
    """The defect: whole subsystems were missing from the one summary table."""
    assert needle in README.read_text(encoding="utf-8"), area


def test_every_named_symbol_in_the_feature_table_is_exported():
    """A table naming something that does not exist is worse than an omission."""

    table = README.read_text(encoding="utf-8")
    section = table.split("## Feature surface", 1)[1].split("\n\n", 3)[1]
    symbols = set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", section))
    # Reflowing the table so the second blank-line-delimited block is prose
    # yields no backticked symbols, zero iterations and a permanently green
    # test. Assert the scan found something before trusting it.
    assert symbols, "the feature-table scan matched no backticked symbol"
    for symbol in symbols:
        if symbol.islower() and symbol not in veloce.__all__:
            continue  # a CLI verb or a decorator spelled without its module
        assert symbol in veloce.__all__ or hasattr(veloce, symbol), symbol


def test_the_readme_lists_every_core_dependency():
    """A reader sizing the install must see all of it."""
    text = README.read_text(encoding="utf-8").lower()
    for spec in _pyproject()["project"]["dependencies"]:
        name = spec.split(">=")[0].split(";")[0].strip().strip('"')
        if name == "uvloop":
            continue  # named separately, as a non-Windows-only dependency
        assert name.lower() in text, name


# ── dependency floors are at or above their security minimum ─────────


@pytest.mark.parametrize(
    ("package", "minimum"),
    [
        ("orjson", (3, 11, 5)),
        ("pydantic", (2, 4, 0)),
        ("python-multipart", (0, 0, 22)),
        ("jinja2", (3, 1, 6)),
        ("gunicorn", (23, 0, 0)),
    ],
)
def test_the_floor_is_at_or_above_the_security_minimum(package: str, minimum: tuple[int, ...]):
    """The defect: a consumer installing at the floor got a vulnerable release."""
    assert _floor(_requirement(package)) >= minimum, package


def test_every_runtime_dependency_declares_a_floor():
    """An unpinned dependency has no floor to audit at all."""
    for spec in _pyproject()["project"]["dependencies"]:
        assert ">=" in spec, spec


def test_the_installed_versions_satisfy_the_floors():
    """The environment must not be below what the project claims to need."""
    for spec in _pyproject()["project"]["dependencies"]:
        name = spec.split(">=")[0].split(";")[0].strip()
        if name == "uvloop":
            continue
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        parts = tuple(int(p) for p in re.findall(r"\d+", installed)[:3])
        assert parts >= _floor(spec), f"{name} {installed} is below {spec}"


def test_the_all_extra_matches_the_individual_extras():
    """`all` drifting from its parts installs less than a reader expects."""
    extras = _pyproject()["project"]["optional-dependencies"]
    combined = {
        spec.split(">=")[0].split("[")[0].split(";")[0].strip()
        for name, specs in extras.items()
        if name != "all"
        for spec in specs
    }
    named_in_all = {
        spec.split(">=")[0].split("[")[0].split(";")[0].strip() for spec in extras["all"]
    }
    assert combined == named_in_all


# ── the documented pinning policy matches the manifest ───────────────


def test_no_runtime_dependency_declares_an_upper_bound():
    """The documented policy: a floor and no ceiling, except for a known
    incompatibility. A speculative ceiling propagates into every downstream
    resolution and can block an application from taking a fix."""
    for spec in _pyproject()["project"]["dependencies"]:
        assert "<" not in spec, spec


def test_the_versions_page_documents_the_floor_policy():
    page = ROOT / "docs/deployment/versions.md"
    text = page.read_text(encoding="utf-8")
    assert "How Veloce pins its own dependencies" in text
    assert "floor and no ceiling" in text


def test_the_floors_quoted_in_the_docs_match_the_manifest():
    """A worked example that drifts from the manifest teaches the wrong number."""
    page = ROOT / "docs/deployment/versions.md"
    section = page.read_text(encoding="utf-8").split("## How Veloce pins its own", 1)[1]
    quoted = dict(re.findall(r'"([a-z0-9-]+)>=([0-9.]+)"', section))
    # Same hazard: if the page stops quoting versions in this style the
    # loop below runs zero times and reports nothing.
    assert quoted, "the versions page quoted no pinned dependency"
    declared = {
        spec.split(">=")[0].strip(): spec.split(">=")[1].split(";")[0].strip()
        for spec in _pyproject()["project"]["dependencies"]
        if ">=" in spec
    }
    for name, version in quoted.items():
        assert declared.get(name) == version, f"{name}: docs say {version}"
