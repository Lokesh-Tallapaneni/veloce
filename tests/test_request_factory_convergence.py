"""The suite builds a `Request` in one place, and does not grow new ones.

`tests/conftest.make_request` existed, and dozens of modules re-derived it as a
private `_req` / `_request` factory anyway - 71 of them returning exactly the
same five-argument `Request(...)` call, under mutually incompatible signatures.
A change to the constructor meant editing all of them, and the shared factory
reached almost none of the hand construction.

Seventy of those now delegate to `make_request`. Their local names, signatures
and call sites are unchanged - only the construction moved - so no test's
meaning changed and the suite stayed at 11,709 passing.

What remains is genuinely different: inline construction inside a single test,
multi-statement factories that mutate the request afterwards, and one positional
construction. Those are not mechanical, so this module holds the line rather
than pretending they are done: **the count may shrink, never grow.** A new module
that hand-builds a `Request` fails here with a pointer to the factory.
"""

from __future__ import annotations

import ast
import pathlib

from veloce import Veloce

TESTS = pathlib.Path(__file__).resolve().parent

#: Modules still constructing `Request(...)` without the shared factory. This is
#: a ceiling, not a target: it is expected to fall as those are converted, and
#: lowering it is the point. It must never be raised.
DIRECT_CONSTRUCTION_CEILING = 68


def _modules_constructing_request() -> list[str]:
    found = []
    for path in sorted(TESTS.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a broken test module fails elsewhere
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Request":
                found.append(path.name)
                break
    return found


def _modules_importing_the_factory() -> list[str]:
    return [
        path.name
        for path in sorted(TESTS.rglob("test_*.py"))
        if "import make_request" in path.read_text(encoding="utf-8")
    ]


# Scanned once. Three tests called `_modules_constructing_request()` and each
# re-parsed all ~750 test modules - three of the suite's six slowest tests, for
# one answer computed three times.
CONSTRUCTING = _modules_constructing_request()
IMPORTING = _modules_importing_the_factory()


# ── the line that must not move the wrong way ────────────────────────


def test_no_new_module_hand_builds_a_request():
    """A new `Request(...)` in a test module is a new copy of the factory.

    If this fails on a module you added: import `make_request` from
    `tests.conftest` instead. If you genuinely need a shape it cannot build, add
    the argument there - it forwards `**extra` to the constructor.
    """
    count = len(CONSTRUCTING)
    assert count <= DIRECT_CONSTRUCTION_CEILING, (
        f"{count} modules construct `Request(...)` directly, ceiling is "
        f"{DIRECT_CONSTRUCTION_CEILING}; use tests.conftest.make_request"
    )


def test_the_ceiling_is_not_stale():
    """A ceiling far above the real count stops being a guard.

    Lower `DIRECT_CONSTRUCTION_CEILING` when you convert a module - that is the
    mechanism by which this shrinks.
    """
    count = len(CONSTRUCTING)
    assert count >= DIRECT_CONSTRUCTION_CEILING - 5, (
        f"only {count} modules construct `Request(...)` directly; lower the "
        f"ceiling from {DIRECT_CONSTRUCTION_CEILING} to {count}"
    )


def test_the_shared_factory_is_the_majority_path():
    """It was imported by 36 modules against ~96 private factories."""
    shared = len(IMPORTING)
    direct = len(CONSTRUCTING)
    assert shared > direct, (shared, direct)


# ── and the factory builds what the callers need ─────────────────────


def test_the_factory_covers_the_five_common_arguments():
    from tests.conftest import make_request

    request = make_request(
        method="POST",
        path="/x",
        headers={"x-a": "1"},
        body=b"hi",
        query_string="q=1",
    )
    assert request.method == "POST"
    assert request.path == "/x"
    assert request.headers["x-a"] == "1"
    assert request.query_params["q"] == "1"


def test_the_factory_forwards_the_uncommon_ones():
    """`**extra` exists so a module needing `app` / `scope` / `transport` does
    not have to fall back to hand construction - which is what created the
    duplication in the first place."""
    from tests.conftest import make_request

    app = Veloce(openapi_url=None)
    assert make_request(path="/x", app=app).app is app


def test_the_defaults_build_a_usable_request():
    from tests.conftest import make_request

    request = make_request()
    assert request.method == "GET"
    assert request.path == "/"
