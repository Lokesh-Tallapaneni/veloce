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
them. It rejects three shapes that are never one of those reasons:

  * an import binding exactly what a module-top import already binds - whether
    from the same place, or by a different path to the same object
    (`veloce.BadRequest` *is* `veloce.exceptions.BadRequest`);
  * a `veloce.*` import inside a body of a module that never calls
    `importorskip` or `pytest.skip`, where there is nothing to defer: `veloce`
    is always importable, so the top of the module could bind it;
  * an import of a **declared runtime dependency** in such a module, for the
    same reason - a module that cannot import `orjson` cannot be collected
    either, so deferring it defers nothing.

A hoist of the second shape moved 410 imports out of 235 modules, and removing
the copies it made redundant took 193 more. The third took eighty `import
orjson` statements out of forty.

An *optional* dependency is the legitimate case and stays allowed: `msgspec`,
`redis` and the ASGI servers may genuinely be absent, which is what
`importorskip` is for. So may `uvloop`, which is not installable on Windows -
it is excluded from the runtime set by name rather than by accident.
"""

from __future__ import annotations

import ast
import pathlib
import re

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


# Distribution name -> the module it provides, for everything in `[project]
# dependencies`. `uvloop` maps to nothing: its marker excludes Windows, so a
# body import of it is a real deferral rather than a habit.
_RUNTIME_MODULES = {
    "uvloop": None,
    "orjson": "orjson",
    "pydantic": "pydantic",
    "multidict": "multidict",
    "python-multipart": "multipart",
    "httptools": "httptools",
    "jinja2": "jinja2",
}
_ALWAYS_PRESENT = frozenset(m for m in _RUNTIME_MODULES.values() if m)


def _declared_runtime_dependencies() -> set[str]:
    """The distribution names in `[project] dependencies`.

    Scanned rather than parsed with `tomllib`, which is 3.11+ while the project
    supports 3.10. The block is a flat list of quoted requirement strings.
    """
    text = (TESTS.parent / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split("\ndependencies = [", 1)[1].split("\n]", 1)[0]
    names = set()
    for line in block.split(chr(10)):
        line = line.strip().strip(",").strip('"')
        if not line or line.startswith("#"):
            continue
        names.add(re.split(r"[<>=!;\[ ]", line, maxsplit=1)[0])
    return names


def _unjustified_runtime_dependency(path: pathlib.Path) -> list[str]:
    """Hard-dependency imports inside a body where nothing could need deferring."""
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
            if not module or module.split(".")[0] not in _ALWAYS_PRESENT:
                continue
            above = lines[sub.lineno - 2].strip() if sub.lineno >= 2 else ""
            if "#" in lines[sub.lineno - 1] or above.startswith("#"):
                continue
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
def test_no_runtime_dependency_import_hides_inside_a_body(path):
    offenders = _unjustified_runtime_dependency(path)
    assert offenders == [], (
        f"{path.name}: these import a declared runtime dependency, which this "
        "module needs to be collected at all, so there is nothing to defer - "
        f"move them to the module top: {offenders}"
    )


def test_the_runtime_module_map_covers_every_declared_dependency():
    """A new dependency has to be classified, not silently unguarded."""
    assert set(_RUNTIME_MODULES) == _declared_runtime_dependencies()


def test_a_hard_dependency_import_in_a_body_is_found(tmp_path):
    module = tmp_path / "test_probe.py"
    module.write_text("def test_x():\n    import orjson\n\n    assert orjson\n", encoding="utf-8")
    assert _unjustified_runtime_dependency(module) == ["test_x:2"]


def test_an_optional_dependency_import_in_a_body_is_allowed(tmp_path):
    module = tmp_path / "test_probe.py"
    module.write_text("def test_x():\n    import msgspec\n\n    assert msgspec\n", encoding="utf-8")
    assert _unjustified_runtime_dependency(module) == []


def test_a_module_that_skips_is_left_alone(tmp_path):
    """`importorskip` anywhere means the module has a deferral story."""
    module = tmp_path / "test_probe.py"
    module.write_text(
        "import pytest\n\n\ndef test_x():\n    pytest.importorskip('redis')\n"
        "    import orjson\n\n    assert orjson\n",
        encoding="utf-8",
    )
    assert _unjustified_runtime_dependency(module) == []


def test_a_commented_deferral_is_allowed(tmp_path):
    module = tmp_path / "test_probe.py"
    module.write_text(
        "def test_x():\n    # deferred so the monkeypatch above is seen\n"
        "    import orjson\n\n    assert orjson\n",
        encoding="utf-8",
    )
    assert _unjustified_runtime_dependency(module) == []


def test_a_module_top_import_is_not_an_offence(tmp_path):
    module = tmp_path / "test_probe.py"
    module.write_text("import orjson\n\n\ndef test_x():\n    assert orjson\n", encoding="utf-8")
    assert _unjustified_runtime_dependency(module) == []


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
        "from veloce import Veloce\n\n\ndef test_x():\n    import msgspec\n\n    assert msgspec\n",
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
