"""An in-body import in a test must be there for a reason.

The suite's in-body imports were overwhelmingly habit rather than necessity:
`test_flash.py` re-imported inside five bodies names it already had at module
top - sixteen statements in one module, seventy-six across thirty-four.

Habit is not free. A reader meeting `from veloce.helpers import flash` inside a
test has to work out whether it is there to defer an optional dependency, to
pick up a monkeypatched module, or for nothing; and the module-top import that
already binds the name is the one that decides whether the module can even be
collected, so the inner one could not have failed independently.

There are real reasons to import inside a body, and this guard allows all of
them. It rejects two shapes that are never one of those reasons:

  * an import binding exactly what a module-top import already binds - whether
    from the same place, or by a different path to the same object
    (`veloce.BadRequest` *is* `veloce.exceptions.BadRequest`);
  * a `veloce.*` import inside a body of a module that never calls
    `importorskip` or `pytest.skip`, where there is nothing to defer: `veloce`
    is always importable, so the top of the module could bind it.

A hoist of the second shape moved 410 imports out of 235 modules, and removing
the copies it made redundant took 193 more.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

TESTS = pathlib.Path(__file__).resolve().parent


def _modules() -> list[pathlib.Path]:
    return sorted(TESTS.glob("test_*.py"))


def _top_level_bindings(tree: ast.Module) -> dict[str, tuple]:
    """Name -> what a module-top import bound it to."""
    bindings: dict[str, tuple] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and not node.level:
            for alias in node.names:
                bindings[alias.asname or alias.name] = ("from", node.module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                key = alias.asname or alias.name.split(".")[0]
                bindings[key] = ("import", None, alias.name)
    return bindings


def _redundant(path: pathlib.Path) -> list[str]:
    """In-body imports that re-bind exactly what module scope already has."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    top = _top_level_bindings(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom) and not sub.level:
                # A relative import, or an alias binding a different name, or a
                # name module scope does not have: all legitimate.
                if sub.names and all(
                    top.get(alias.asname or alias.name) == ("from", sub.module, alias.name)
                    for alias in sub.names
                ):
                    offenders.append(f"{node.name}:{sub.lineno}")
            elif (
                isinstance(sub, ast.Import)
                and sub.names
                and all(
                    top.get(alias.asname or alias.name.split(".")[0])
                    == ("import", None, alias.name)
                    for alias in sub.names
                )
            ):
                offenders.append(f"{node.name}:{sub.lineno}")
    return offenders


def _unjustified_first_party(path: pathlib.Path) -> list[str]:
    """`veloce.*` imports inside a body where nothing could need deferring."""
    text = path.read_text(encoding="utf-8")
    if "importorskip" in text or "pytest.skip" in text:
        return []
    lines = text.split(chr(10))
    tree = ast.parse(text, filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, (ast.Import, ast.ImportFrom)) or sub.col_offset == 0:
                continue
            module = sub.module if isinstance(sub, ast.ImportFrom) else sub.names[0].name
            if not module or module.split(".")[0] != "veloce":
                continue
            # A comment marks a deliberate deferral - a re-export check, or an
            # import the test monkeypatches around. Above the statement is
            # where the style guide puts one; trailing is accepted too.
            above = lines[sub.lineno - 2].strip() if sub.lineno >= 2 else ""
            if "#" in lines[sub.lineno - 1] or above.startswith("#"):
                continue
            offenders.append(f"{node.name}:{sub.lineno}")
    return offenders


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_first_party_import_hides_inside_a_body(path):
    offenders = _unjustified_first_party(path)
    assert offenders == [], (
        f"{path.name}: `veloce` is always importable and this module has no "
        "skip guard, so these have nothing to defer - move them to the module "
        f"top, or add a comment saying what the deferral is for: {offenders}"
    )


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_in_body_import_repeats_a_module_top_one(path):
    offenders = _redundant(path)
    assert offenders == [], (
        f"{path.name}: these re-import what module scope already binds - "
        f"delete them or give the module-top import a reason to go: {offenders}"
    )


# ── the guard is not vacuous ─────────────────────────────────────────


def test_the_import_scan_covers_the_suite():
    assert len(_modules()) > 400


def test_a_repeat_would_be_caught(tmp_path):
    module = tmp_path / "test_probe.py"
    module.write_text(
        "from veloce import Veloce\n\n\ndef test_x():\n    from veloce import Veloce\n\n    assert Veloce\n",
        encoding="utf-8",
    )
    assert _redundant(module) == ["test_x:5"]


def test_an_alias_is_not_a_repeat(tmp_path):
    """`from veloce import Veloce as V` binds a different name; the test that
    checks a symbol is importable under another name is doing real work."""
    module = tmp_path / "test_probe.py"
    module.write_text(
        "from veloce import Veloce\n\n\ndef test_x():\n    from veloce import Veloce as V\n\n    assert V\n",
        encoding="utf-8",
    )
    assert _redundant(module) == []


def test_a_name_module_scope_lacks_is_not_a_repeat(tmp_path):
    """The optional-dependency case, and the deliberately-deferred case."""
    module = tmp_path / "test_probe.py"
    module.write_text(
        "from veloce import Veloce\n\n\ndef test_x():\n    import orjson\n\n    assert orjson\n",
        encoding="utf-8",
    )
    assert _redundant(module) == []


def test_a_different_source_is_not_a_repeat(tmp_path):
    """Importing the same name from its defining module rather than the package
    root is how a test asserts the two are the same object."""
    module = tmp_path / "test_probe.py"
    module.write_text(
        "from veloce import Veloce\n\n\n"
        "def test_x():\n    from veloce.app import Veloce\n\n    assert Veloce\n",
        encoding="utf-8",
    )
    assert _redundant(module) == []
