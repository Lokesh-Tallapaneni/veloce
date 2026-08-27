"""A deferred import that breaks a cycle says so, and the cycle is real.

The style guide sanctions one inline comment form for a forced deferred import:
`from x import y  # breaks a->b cycle`. That note is the only thing separating a
load-bearing deferral from an import somebody moved for no reason - so hoisting
it back would look safe right up until the package stopped importing.

An earlier guard for this computed its answer in a loop and then gated its only
assertion behind a re-scan for the same import, so it no-opped if the pinned
import moved. This one parametrizes over what the scan found and asserts a
non-empty scan separately, which is the shape that cannot go quiet.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce"
NOTE = re.compile(r"#.*\bbreaks\b.*\bcycle\b", re.I)


def _noted_deferrals() -> list[tuple[str, int, str]]:
    """Every in-function import whose line carries a cycle note."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").split("\n")
        tree = ast.parse("\n".join(lines))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, (ast.Import, ast.ImportFrom)) or sub.col_offset == 0:
                    continue
                text = "\n".join(lines[sub.lineno - 1 : sub.end_lineno])
                if NOTE.search(text):
                    module = sub.module if isinstance(sub, ast.ImportFrom) else sub.names[0].name
                    found.append((str(path.relative_to(SRC)), sub.lineno, module or ""))
    return found


NOTED = _noted_deferrals()


def test_the_scan_found_the_noted_deferrals() -> None:
    """Without this, an empty scan would make the check below pass silently."""
    assert len(NOTED) >= 4, f"expected several noted cycle-breaking imports, found {NOTED}"


@pytest.mark.parametrize(
    ("where", "line", "module"),
    NOTED,
    ids=[f"{w}:{ln}" for w, ln, _ in NOTED],
)
def test_the_noted_import_targets_a_real_module(where: str, line: int, module: str) -> None:
    """A note naming a module that no longer exists is worse than no note."""
    import importlib

    assert module.startswith("veloce"), f"{where}:{line} notes a non-veloce import"
    importlib.import_module(module)


def test_the_cycle_is_still_there_to_break() -> None:
    """The deferral must be load-bearing: hoisting it would close a loop.

    Checked through the module-level import graph the sibling guard builds - if
    a noted import could be hoisted without creating a cycle, the note is stale.
    """
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from test_module_import_graph import _graph, _module_name

    graph = _graph(include_gateway=True)
    stale: list[str] = []
    for where, line, module in NOTED:
        owner = _module_name(SRC / where)
        # Would adding this edge reach `owner` again?
        seen, stack = set(), [module]
        while stack:
            current = stack.pop()
            if current == owner:
                break
            if current in seen:
                continue
            seen.add(current)
            stack.extend(graph.get(current, ()))
        else:
            stale.append(f"{where}:{line} -> {module}")
    assert stale == [], (
        "these imports are noted as breaking a cycle, but hoisting them would "
        f"not close one - the note is stale: {stale}"
    )
