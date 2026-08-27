"""A deferred import of a Veloce module says which cycle it breaks.

An import inside a function body is a `sys.modules` lookup on every call, and it
is invisible to static analysis. The project rule allows one only to break a
circular import, and requires a comment naming the cycle — so the next reader
inherits a decision rather than an unexplained line.

Two in `_handler_plan.py` carried no note at all, in a file where the other two
did. One of them turned out to be load-bearing after all and is now documented;
the other, and two more elsewhere, hoisted cleanly and are gone.

`KNOWN_UNDOCUMENTED` freezes what is left. The list may shrink; a new entry
fails, which is the point.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce"

#: The two legitimate reasons to defer an intra-package import, either of which
#: must appear in a comment within six lines above it.
#:
#: A **cycle**: two modules need each other and the import cannot be hoisted
#: without breaking one of them. The comment must name the cycle.
#:
#: A **layer**: `app/` is core and `contrib/` is optional, so importing an
#: optional integration eagerly makes every `import veloce` pay for machinery
#: most apps never mount. These deferrals are registration-time and run once.
#: This category was not expressible before, which is why seven `app/ ->
#: contrib/` sites sat on the frozen list below as though they were unexplained.
_ACCEPTED_REASONS = ("cycle", "circular", "layering", "optional integration")

#: Deferred imports of Veloce modules that carry neither note yet. Each is a
#: `path::module` pair. Removing one is an improvement; adding one is a
#: regression and fails `test_no_new_undocumented_deferred_import`.
KNOWN_UNDOCUMENTED: frozenset[str] = frozenset(
    {
        "__init__.py::veloce._version",
        "app/core.py::veloce",
        "app/core.py::veloce.blueprints",
        "app/core.py::veloce.config",
        "app/core.py::veloce.json_provider",
        "app/lifecycle.py::veloce.watchdog",
        "app/serving.py::veloce.serving.protocol",
        "app/serving.py::veloce.serving.reloader",
        "app/testing.py::veloce.testclient",
        "cli.py::veloce._scaffold",
        "cli.py::veloce.contrib.mcp.server",
        "contrib/staticfiles.py::veloce.audit",
        "exceptions.py::veloce.helpers",
        "testclient.py::veloce.middleware.sessions",
        "testclient.py::veloce.sessions",
        "workers.py::veloce.serving.protocol",
    }
)


def _deferred_veloce_imports() -> dict[str, list[int]]:
    """Every function-body import of a `veloce.*` module, and whether it is noted."""
    found: dict[str, list[int]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        tree = ast.parse("\n".join(lines))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, (ast.Import, ast.ImportFrom)):
                    continue
                module = getattr(sub, "module", None) or (sub.names[0].name if sub.names else "")
                if not module.startswith("veloce"):
                    continue
                window = " ".join(lines[max(0, sub.lineno - 6) : sub.lineno]).lower()
                if any(word in window for word in _ACCEPTED_REASONS):
                    continue
                key = f"{path.relative_to(PACKAGE).as_posix()}::{module}"
                found.setdefault(key, []).append(sub.lineno)
    return found


# ── the guard ────────────────────────────────────────────────────────


def test_no_new_undocumented_deferred_import():
    """A new one is a regression; the frozen list may only shrink."""
    new = sorted(set(_deferred_veloce_imports()) - KNOWN_UNDOCUMENTED)
    assert new == [], (
        "deferred imports of a veloce module with no comment naming the cycle "
        f"they break: {new}. Hoist it, or say which cycle it breaks."
    )


def test_the_frozen_list_names_nothing_that_is_already_fixed():
    """A stale entry hides the fact that the site was cleaned up."""
    stale = sorted(KNOWN_UNDOCUMENTED - set(_deferred_veloce_imports()))
    assert stale == [], f"already documented or hoisted, remove from the list: {stale}"


# ── the sites this change touched ────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "module"),
    [
        ("_handler_plan.py", "veloce.websocket"),
        ("dependency.py", "veloce._handler_plan"),
        ("_route_contract.py", "veloce._handler_plan"),
    ],
)
def test_a_hoisted_import_is_at_module_scope(path, module):
    """Proven safe by hoisting and running the suite, so it is not deferred."""
    source = (PACKAGE / path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level = {
        node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
    }
    assert module in top_level


@pytest.mark.parametrize(
    ("path", "module"),
    [
        ("_handler_plan.py", "veloce.exceptions"),
        ("routing/router.py", "veloce._handler_plan"),
        ("dependency.py", "veloce._handler_plan"),
    ],
)
def test_a_load_bearing_deferred_import_names_its_cycle(path, module):
    """These three genuinely cycle; the rule is that they say so."""
    lines = (PACKAGE / path).read_text(encoding="utf-8").splitlines()
    noted = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"from {module} import") and line.startswith(" "):
            window = " ".join(lines[max(0, i - 6) : i]).lower()
            if "cycle" in window or "circular" in window:
                noted = True
    if any(
        line.strip().startswith(f"from {module} import") and line.startswith(" ") for line in lines
    ):
        assert noted, f"{path} defers {module} without naming the cycle"


# ── the package still imports, which is the real check ───────────────


def test_the_package_imports_cleanly():
    """Hoisting an import is exactly the change that breaks this."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import veloce; print(veloce.__name__)"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_every_module_imports_on_its_own():
    """A cycle can hide until a submodule is imported first, without the package."""
    import subprocess
    import sys

    for module in (
        "veloce._handler_plan",
        "veloce.dependency",
        "veloce._route_contract",
        "veloce.routing.router",
        "veloce.websocket",
        "veloce.exceptions",
    ):
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"], capture_output=True, text=True
        )
        assert result.returncode == 0, f"{module}: {result.stderr}"


#
# Moved here from `test_unswept_scope_findings.py`, a module named for the audit
# batch that produced it rather than for the source it covers.


def test_the_scope_defers_only_the_optional_dependency():
    """No deferred import in this scope claims to break a cycle."""
    root = pathlib.Path(__file__).resolve().parents[1] / "src/veloce"
    deferred = []
    for directory in ("routing", "security", "serving", "middleware"):
        for path in (root / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in [
                n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]:
                for sub in ast.walk(fn):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        deferred.append(getattr(sub, "module", None) or sub.names[0].name)
    # `watchfiles` for the reloader, and the compression codecs, whose packages
    # are optional. Each of those is imported once at module import to build the
    # codec table, not per response - the point of the rule is that no deferred
    # import exists to paper over a cycle, and none of these does.
    assert set(deferred) <= {"watchfiles", "brotli", "brotlicffi", "zstandard"}, deferred
