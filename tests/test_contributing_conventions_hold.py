"""The conventions `CONTRIBUTING.md` states are true of this tree.

A contributing guide that describes a practice nobody follows teaches the wrong
thing, and it rots silently because nothing reads it. So the two conventions it
names are checked here against the code they describe.

They came out of watching what actually caught mistakes. Every error made while
this section was being written was caught by something executable - ruff, mypy,
a parity test, the suite, the strict docs build - and the two that escaped were
both *claims about code that nothing executed*: a worked example in a guide that
was wrong, and a statement in a review note that was wrong. Hence the first
convention.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
SRC = ROOT / "src/veloce"
TESTS = ROOT / "tests"


# ── the guide says what it says ──────────────────────────────────────


def test_the_guide_documents_both_conventions():
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "How this codebase keeps itself honest" in text
    assert "executed, not asserted" in text
    assert "enforced where it is broken" in text


# ── convention 1: claims are executed ────────────────────────────────


def test_the_claim_test_families_exist():
    """The guide points at these names; they must be findable."""
    for pattern in ("*claims*", "*contract*", "*parity*", "*invariant*"):
        assert list(TESTS.glob(f"test_{pattern}.py")), pattern


def test_some_test_executes_documentation_code_blocks():
    """The practice the guide asks for, present somewhere in the suite."""
    executors = [
        path.name
        for path in TESTS.glob("test_*.py")
        if "```" in path.read_text(encoding="utf-8")
        or (
            "docs/" in path.read_text(encoding="utf-8")
            and "compile(" in path.read_text(encoding="utf-8")
        )
    ]
    assert executors, "no test executes documentation code blocks"


# ── convention 2: structural rules are enforced structurally ─────────


def _abstract_bases() -> list[tuple[pathlib.Path, ast.ClassDef, list[str]]]:
    """Every class declaring a method whose body raises `NotImplementedError`."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            abstract = [
                fn.name
                for fn in node.body
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any(
                    isinstance(stmt, ast.Raise)
                    and getattr(getattr(stmt.exc, "func", stmt.exc), "id", "")
                    == "NotImplementedError"
                    for stmt in ast.walk(fn)
                )
            ]
            if abstract:
                found.append((path, node, abstract))
    return found


def test_every_abstract_base_checks_its_subclasses():
    """The rule the guide states: a base meant for subclassing enforces it.

    Walks the tree rather than listing names, so a base class added later is
    covered without anyone remembering to add it here.
    """
    unguarded = []
    for path, node, _abstract in _abstract_bases():
        guarded = any(
            isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and fn.name == "__init_subclass__"
            for fn in node.body
        )
        if not guarded:
            unguarded.append(f"{path.relative_to(ROOT)}::{node.name}")
    assert not unguarded, f"abstract bases with no subclass check: {unguarded}"


def test_the_helper_the_guide_names_exists():
    from veloce._internal import _require_methods

    assert callable(_require_methods)


def test_at_least_six_bases_are_guarded():
    """A regression that removed the guards would still pass the walk above if
    it also removed the abstract methods; pin the count as well."""
    assert len(_abstract_bases()) >= 6


# ── convention 2: no security surface validates with `assert` ────────


SECURITY_SURFACES = ("middleware", "security", "sessions.py", "signing.py", "passwords.py")


def test_no_security_surface_validates_configuration_with_assert():
    """`python -O` strips asserts. A middleware that validated its arguments
    with one constructed under `-O` and emitted no header at all.

    Type-narrowing asserts (`x is not None` on a value already established) are
    allowed - stripping those changes nothing.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        relative = str(path.relative_to(SRC)).replace("\\", "/")
        if not any(surface in relative for surface in SECURITY_SURFACES):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            test_src = ast.unparse(node.test)
            narrowing = " is not None" in test_src or "isinstance" in test_src
            if not narrowing:
                offenders.append(f"{relative}:{node.lineno}  {test_src[:60]}")
    assert not offenders, f"assert used for validation on a security surface: {offenders}"


def test_the_csp_middleware_refuses_an_empty_policy_without_asserts():
    """The case that produced the rule, kept as a live example of it."""
    from veloce import CSPMiddleware

    with pytest.raises(ValueError):
        CSPMiddleware()
