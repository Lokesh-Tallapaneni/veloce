"""No subpackage reaches into another subpackage's private names.

`.claude/rules/development-guardrails.md` ("Cross-Subpackage Imports") says a
leading-underscore symbol must not be imported across a subpackage boundary: if
two subpackages need it, it is not private, and it belongs in `veloce._internal`
- the documented carve-out, which this test exempts.

The rule had no enforcing test, so the one violation it existed to prevent sat
in the tree unnoticed while two separate docstrings asserted it had been removed.
An AST walk is the check, not a grep: a grep for the name finds prose and
comments too, which is how the false prose survived review.

Scope, stated so a later reader does not mistake a pass for more than it proves:
this checks subpackage -> *different* subpackage only. `veloce/foo.py` at the
top level is not a subpackage, so `blueprints.py` importing `_endpoint_blueprint`
is out of scope here, as is a subpackage importing from a top-level module.
Widening it is a deliberate decision, not an oversight to be silently fixed.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce"

# The documented home for internals shared across subpackages.
SANCTIONED = "veloce._internal"


def _subpackage_of(path: pathlib.Path) -> str | None:
    """The subpackage a source file belongs to, or `None` for a top-level module."""
    parts = path.relative_to(SRC).parts
    return parts[0] if len(parts) > 1 else None


def _violations() -> list[str]:
    found: list[str] = []
    for file in sorted(SRC.rglob("*.py")):
        if "__pycache__" in file.parts:
            continue
        owner = _subpackage_of(file)
        if owner is None:
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module == SANCTIONED or node.module.startswith(SANCTIONED + "."):
                continue
            target = node.module.split(".")
            if len(target) < 2 or target[0] != "veloce":
                continue
            source = target[1]
            # A dotted module name whose second segment is a subpackage; a
            # top-level module such as `veloce.blueprints` is out of scope.
            if not (SRC / source).is_dir() or source == owner:
                continue
            for alias in node.names:
                if alias.name.startswith("_"):
                    rel = file.relative_to(SRC.parent.parent)
                    found.append(f"{rel}:{node.lineno} imports {alias.name} from {node.module}")
    return found


def test_no_subpackage_imports_another_subpackages_private_names():
    assert _violations() == []


def test_the_walk_would_notice_a_violation():
    """The check itself must be able to fail.

    A structural test that reports "no violations" is indistinguishable from one
    whose walk silently matches nothing, so this pins that the same predicate
    flags a synthetic offender.
    """
    tree = ast.parse("from veloce.routing.router import _readd_route\n")
    node = tree.body[0]
    assert isinstance(node, ast.ImportFrom)
    assert node.module is not None
    source = node.module.split(".")[1]
    assert (SRC / source).is_dir()
    assert [a.name for a in node.names if a.name.startswith("_")] == ["_readd_route"]
