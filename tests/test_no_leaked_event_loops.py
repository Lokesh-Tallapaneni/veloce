"""A test that makes an event loop closes it.

`asyncio.new_event_loop()` allocates a selector and its file descriptors, and a
loop that is never closed holds them for the rest of the session. Twenty-one
sync functions across the suite did that - and almost none of them needed a loop
of their own: `asyncio_mode = "auto"` means an `async def` test gets one from
pytest-asyncio, which closes it.

Three sites genuinely stay synchronous, because they pair the sync `TestClient`
(which drives its own loop) with a direct call into the app - and a sync client
cannot run inside an already-running loop. They use `asyncio.run`, which creates
a loop and closes it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

TESTS = pathlib.Path(__file__).resolve().parent
MAKES_LOOP = ("new_event_loop()", "get_event_loop_policy()")
CLOSES = (".close()", "asyncio.run(")


def _functions_that_make_a_loop() -> list[tuple[str, str, int, str]]:
    """Every function that creates a loop, with the text its closing is judged on.

    A method of a class that closes the loop elsewhere - `NativeClient` makes
    one in `__init__` and closes it in `close()`, which `__exit__` calls - is
    judged on the whole class, because that is where the lifetime is. A plain
    function is judged on itself.
    """
    found = []
    for path in sorted(TESTS.glob("*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        if not any(marker in text for marker in MAKES_LOOP):
            continue
        tree = ast.parse(text)
        owner: dict[int, ast.ClassDef] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    owner[id(child)] = node
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.unparse(node)
            if not any(marker in body for marker in MAKES_LOOP):
                continue
            enclosing = owner.get(id(node))
            judged = ast.unparse(enclosing) if enclosing is not None else body
            found.append((path.name, node.name, node.lineno, judged))
    return found


SITES = _functions_that_make_a_loop()


def test_the_scan_finds_the_sites_it_is_meant_to() -> None:
    """A scan that matches nothing would make the check below vacuous."""
    assert SITES, "no function in the suite creates an event loop - is the scan working?"


@pytest.mark.parametrize(
    ("module", "name", "line"),
    [(m, n, ln) for m, n, ln, _ in SITES],
    ids=[f"{m}:{n}" for m, n, _, _ in SITES],
)
def test_a_created_loop_is_closed(module: str, name: str, line: int) -> None:
    body = next(b for m, n, _, b in SITES if m == module and n == name)
    assert any(marker in body for marker in CLOSES), (
        f"{module}:{line} {name} creates an event loop and nothing closes it. "
        "Make the test `async def` and `await` instead - pytest-asyncio owns "
        "the loop and closes it - or use `asyncio.run`, which does both. A "
        "helper class that owns a loop for its lifetime needs a `close()`."
    )


def test_the_closed_check_can_actually_fail() -> None:
    """The classifier, on the two shapes it exists to tell apart."""
    leaks = "def t():\n    asyncio.new_event_loop().run_until_complete(x())\n"
    closes = "def t():\n    asyncio.run(x())\n"
    assert any(m in leaks for m in MAKES_LOOP)
    assert not any(m in leaks for m in CLOSES)
    assert any(m in closes for m in CLOSES)
