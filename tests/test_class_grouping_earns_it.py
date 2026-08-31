"""A `Test*` class in a module of bare functions has a reason to be one.

The house convention is bare functions. A class is not wrong - grouping tests
that share a fixture, a teardown, or a helper is what a class is for - but a
class holding nothing but `def test_...(self)` adds a level of indentation and
takes away nothing, and fifty of them had accumulated as a second era of tests
sitting inside the first.

The rule this enforces is the useful half: in a module that also has bare
functions, a class must contribute something. A fixture, a `setup_method` /
`teardown_method`, a helper, a class attribute, `self` used by a test, a
decorated method - or a docstring saying what the group is, which is the reason
a reader actually needs.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

TESTS = pathlib.Path(__file__).resolve().parent


def _contributes(cls: ast.ClassDef) -> str | None:
    """What the class gives a reader, or `None` if it is only indentation."""
    if ast.get_docstring(cls) is not None:
        return "docstring"
    for node in cls.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            return "class attribute"
        if isinstance(node, ast.ClassDef):
            return "nested class"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("test_"):
                return "helper or fixture"
            if node.decorator_list:
                return "decorated method"
            if "self." in ast.unparse(node):
                return "uses self"
    return None


def _mixed_modules() -> list[tuple[pathlib.Path, ast.ClassDef]]:
    found = []
    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bare = [
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
        ]
        if not bare:
            continue  # a module of classes throughout is its own consistent choice
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef) and cls.name.startswith("Test"):
                found.append((path, cls))
    return found


CLASSES = _mixed_modules()


def test_the_scan_finds_classes_to_judge() -> None:
    """An empty scan would make the parametrized check below vacuous."""
    assert CLASSES, "no Test* class in any mixed module - is the scan working?"


@pytest.mark.parametrize(
    ("module", "name", "line"),
    [(p.name, c.name, c.lineno) for p, c in CLASSES],
    ids=[f"{p.name}::{c.name}" for p, c in CLASSES],
)
def test_the_class_earns_its_grouping(module: str, name: str, line: int) -> None:
    cls = next(c for p, c in CLASSES if p.name == module and c.name == name)
    assert _contributes(cls) is not None, (
        f"{module}:{line} {name} holds only `def test_...(self)` in a module that "
        "otherwise uses bare functions, so it adds indentation and nothing else. "
        "Flatten it, or give it the fixture, helper or docstring that makes the "
        "grouping mean something."
    )


def test_the_classifier_tells_the_two_shapes_apart() -> None:
    """The judgement, on a minimal example of each."""
    empty = ast.parse("class TestX:\n    def test_a(self):\n        assert True\n").body[0]
    with_helper = ast.parse(
        "class TestY:\n    def _build(self):\n        ...\n    def test_a(self):\n        assert True\n"
    ).body[0]
    assert _contributes(empty) is None
    assert _contributes(with_helper) == "helper or fixture"
