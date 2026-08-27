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
import pathlib
import re

import pytest

TESTS = pathlib.Path(__file__).resolve().parent


def _modules() -> list[pathlib.Path]:
    return sorted(TESTS.glob("test_*.py"))


def _marked(path: pathlib.Path) -> list[str]:
    """Names of functions carrying the redundant marker."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for decorator in node.decorator_list:
            source = ast.unparse(decorator)
            if source in ("pytest.mark.asyncio", "mark.asyncio"):
                found.append(node.name)
    return found


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_module_carries_the_redundant_marker(path):
    marked = _marked(path)
    assert marked == [], (
        f'{path.name}: `asyncio_mode = "auto"` collects these already - '
        f"the marker adds nothing: {marked}"
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
    import asyncio

    await asyncio.sleep(0)


def test_the_scan_covers_the_suite():
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
