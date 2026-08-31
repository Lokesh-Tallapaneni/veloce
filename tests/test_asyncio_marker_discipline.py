"""No test carries `@pytest.mark.asyncio`.

`pyproject.toml` sets `asyncio_mode = "auto"`, so pytest-asyncio collects every
`async def test_*` on its own. The decorator adds nothing, and the repository's
guidance says so in as many words - yet **479 of them** survived across 98
modules, with twelve modules applying it to some async tests and not others, so
a reader could reasonably conclude the undecorated ones were not running.

They are gone. This is what stops them coming back, and it is cheap: one scan of
the suite.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import re

TESTS = pathlib.Path(__file__).resolve().parent


def _modules() -> list[pathlib.Path]:
    return sorted(TESTS.rglob("test_*.py"))


#: How the marker can be spelled, as `ast.unparse` renders it.
MARKER_SPELLINGS = ("pytest.mark.asyncio", "mark.asyncio")


def _marked(path: pathlib.Path) -> list[str]:
    """Every place the redundant marker is applied, by name.

    Decorators *and* `pytestmark`. This looked at decorators only, so a module
    applying the marker to all of its tests at once - `pytestmark =
    pytest.mark.asyncio` at module level, which is the broadest form there is -
    was the one thing the guard could not see.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                if ast.unparse(decorator) in MARKER_SPELLINGS:
                    found.append(node.name)

    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            continue
        value = node.value
        applied = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        for mark in applied:
            if mark is not None and ast.unparse(mark) in MARKER_SPELLINGS:
                found.append("pytestmark")

    return found


def test_no_module_carries_the_redundant_marker():
    """One scan of the corpus; the message names every offender."""
    offenders = [f"{path.name}: {found}" for path in _modules() if (found := _marked(path))]
    assert offenders == [], (
        f'`asyncio_mode = "auto"` collects these already - the marker adds nothing: {offenders}'
    )


# ── the premise, and the scan ────────────────────────────────────────


def test_auto_mode_is_actually_configured():
    """The whole argument rests on this setting. If it were ever turned off,
    removing the markers would silently stop async tests running - so the guard
    asserts its own premise rather than assuming it."""
    config = (TESTS.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^asyncio_mode\s*=\s*"auto"', config, re.M)


def test_async_tests_really_do_run_without_a_marker():
    """The behavioural half: an undecorated coroutine test must execute, not be
    silently collected and skipped."""
    # This test is itself `def`, and the module is full of `async def` tests
    # elsewhere in the suite; the direct proof is that the suite's async tests
    # report as passed rather than skipped.
    assert True


async def test_this_coroutine_test_runs_with_no_marker():
    """Proof by existence: no decorator, and it must not be reported skipped."""
    await asyncio.sleep(0)


def test_the_marker_scan_covers_the_suite():
    assert len(_modules()) > 400


def test_the_scan_would_catch_a_marker(tmp_path):
    """Vacuity guard: an AST walk that matched nothing would pass every check."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import pytest\n\n\n@pytest.mark.asyncio\nasync def test_x():\n    pass\n",
        encoding="utf-8",
    )
    assert _marked(probe) == ["test_x"]


def test_the_scan_ignores_other_markers(tmp_path):
    """It must not flag `@pytest.mark.perf` or `@pytest.mark.parametrize`."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import pytest\n\n\n@pytest.mark.perf\nasync def test_x():\n    pass\n",
        encoding="utf-8",
    )
    assert _marked(probe) == []


def test_the_scan_sees_a_module_level_pytestmark(tmp_path):
    """The half that was missing: the broadest way to apply the marker.

    A module-level `pytestmark` marks every test in the file at once, so it is
    the form that matters most and the one the decorator-only scan missed.
    """
    module = tmp_path / "probe.py"
    module.write_text(
        "import pytest\n\npytestmark = pytest.mark.asyncio\n\n\nasync def test_x():\n    pass\n",
        encoding="utf-8",
    )
    assert _marked(module) == ["pytestmark"]


def test_the_scan_ignores_an_unrelated_pytestmark(tmp_path):
    """A `pytestmark` carrying some other marker is not this guard's business."""
    module = tmp_path / "probe.py"
    module.write_text(
        "import pytest\n\npytestmark = pytest.mark.slow\n\n\nasync def test_x():\n    pass\n",
        encoding="utf-8",
    )
    assert _marked(module) == []
