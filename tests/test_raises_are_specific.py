"""A `pytest.raises` names the failure it is asserting, or says why it cannot.

`pytest.raises(Exception)` with no `match=` passes on a typo, an import error,
or an unrelated failure - so a test named for a refusal proves only that
something went wrong on the way. Twenty of them were in the suite; most named a
specific exception once someone looked.

Five could not, for reasons worth keeping: the aggregate a teardown failure
raises differs by interpreter (3.11+ groups, 3.10 chains), and one loader's
exception is the caller's callable's rather than Veloce's. Those carry a
comment saying so, which is what this module requires - broad is allowed,
unexplained is not.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

TESTS = pathlib.Path(__file__).resolve().parent
BROAD = {"Exception", "BaseException"}


def _broad_raises() -> list[tuple[str, int, bool]]:
    found = []
    for path in sorted(TESTS.rglob("test_*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func) not in ("pytest.raises", "raises"):
                continue
            if not node.args or ast.unparse(node.args[0]) not in BROAD:
                continue
            if any(keyword.arg == "match" for keyword in node.keywords):
                continue
            explained = "#" in lines[node.lineno - 1] or lines[node.lineno - 2].strip().startswith(
                "#"
            )
            found.append((path.name, node.lineno, explained))
    return found


SITES = _broad_raises()


def test_the_scan_is_looking_at_something() -> None:
    """The parametrized check below is vacuous if the scan finds nothing."""
    assert SITES, "no broad `pytest.raises` found at all - is the scan working?"


@pytest.mark.parametrize(
    ("module", "line"),
    [(m, ln) for m, ln, _ in SITES],
    ids=[f"{m}:{ln}" for m, ln, _ in SITES],
)
def test_a_broad_raises_says_why_it_is_broad(module: str, line: int) -> None:
    explained = next(e for m, ln, e in SITES if m == module and ln == line)
    assert explained, (
        f"{module}:{line} asserts `pytest.raises(Exception)` with no `match=` and "
        "no comment. It will pass on a typo or an unrelated failure. Name the "
        "exception, add `match=`, or say in a comment why neither is possible."
    )


def test_the_classifier_tells_the_two_apart() -> None:
    specific = ast.parse("pytest.raises(ValueError)").body[0].value
    broad = ast.parse("pytest.raises(Exception)").body[0].value
    matched = ast.parse('pytest.raises(Exception, match="x")').body[0].value
    assert ast.unparse(specific.args[0]) not in BROAD
    assert ast.unparse(broad.args[0]) in BROAD
    assert any(k.arg == "match" for k in matched.keywords)
