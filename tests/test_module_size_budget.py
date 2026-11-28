"""No source module grows past the size budget.

A module over 800 lines carries more than one responsibility, which is what the
package splits removed. `LEGACY` records the files that already exceed the
budget, each at its current ceiling: they may shrink, never grow, and a new
module may not join the list.
"""

from __future__ import annotations

import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce"

BUDGET = 800

#: path (relative to `src/veloce`) -> the line count it must not exceed. The
#: ceilings include the Args/Returns/Raises documentation on the public API.
LEGACY: dict[str, int] = {
    "__init__.py": 900,  # the lazy export table lists every public name once
    "_dependency_resolver.py": 880,
    "cli.py": 880,
    "contrib/mcp/context.py": 880,  # public-API docstrings, not logic
    "contrib/mcp/authorization.py": 840,
    "helpers.py": 880,  # grew past the budget only through Args/Returns/Raises sections
    "http/datastructures.py": 1050,
    "middleware/security.py": 1060,
    "middleware/sessions.py": 960,
    "websocket/connection.py": 1090,
}


def _lines(path: pathlib.Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_no_new_module_exceeds_the_budget():
    over = [
        f"{rel}: {n}"
        for path in sorted(SRC.rglob("*.py"))
        if (n := _lines(path)) > BUDGET and (rel := path.relative_to(SRC).as_posix()) not in LEGACY
    ]
    assert over == [], f"modules over {BUDGET} lines (split them): {over}"


def test_a_legacy_module_does_not_grow():
    grown = [
        f"{rel}: {_lines(SRC / rel)} > {cap}"
        for rel, cap in LEGACY.items()
        if _lines(SRC / rel) > cap
    ]
    assert grown == [], f"modules grew past their recorded ceiling: {grown}"


def test_the_legacy_list_has_no_stale_entries():
    stale = [rel for rel in LEGACY if not (SRC / rel).exists() or _lines(SRC / rel) <= BUDGET]
    assert stale == [], f"no longer over {BUDGET} lines - remove from LEGACY: {stale}"
