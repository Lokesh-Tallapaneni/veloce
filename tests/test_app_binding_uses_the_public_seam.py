"""Tests bind the current app with `app.app_context()`, not the contextvar.

`_current_app_var` is private. Nineteen sites across four modules set it with a
hand-written try/finally, which works right up until one forgets the `finally` -
and then the binding leaks into every later test in the session, where it looks
like an unrelated failure.

`app.app_context()` is public, documented for exactly this, nestable, and
restores the previous binding on exit. Two modules read the contextvar to assert
it is *unset*, which is the one use a public API cannot express.
"""

from __future__ import annotations

import pathlib

import pytest

from veloce import Veloce
from veloce._internal import _current_app_var

TESTS = pathlib.Path(__file__).resolve().parent
PRIVATE = "_current_app_var"


def _sites() -> list[tuple[str, int, str]]:
    found = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        lines = path.read_text(encoding="utf-8").split("\n")
        for number, line in enumerate(lines, start=1):
            if PRIVATE in line:
                found.append((path.name, number, line.strip()))
    return found


SITES = _sites()


@pytest.mark.parametrize(
    ("module", "line", "text"),
    SITES,
    ids=[f"{m}:{n}" for m, n, _ in SITES] or ["none"],
)
def test_the_contextvar_is_only_ever_read(module: str, line: int, text: str) -> None:
    """Setting it by hand is what leaks; reading it to assert `None` does not."""
    assert ".set(" not in text and ".reset(" not in text, (
        f"{module}:{line} binds the app through the private `{PRIVATE}`: {text!r}. "
        "Use `with app.app_context():` - it restores the previous binding on "
        "exit whether or not the body raises."
    )


def test_app_context_actually_restores_the_previous_binding() -> None:
    """The property the hand-written version had to remember."""

    outer = Veloce(openapi_url=None)
    inner = Veloce(openapi_url=None)

    assert _current_app_var.get() is None
    with outer.app_context():
        assert _current_app_var.get() is outer
        with inner.app_context():
            assert _current_app_var.get() is inner
        assert _current_app_var.get() is outer, "the nested exit did not restore"
    assert _current_app_var.get() is None


def test_app_context_restores_even_when_the_body_raises() -> None:

    app = Veloce(openapi_url=None)
    with pytest.raises(ValueError, match="boom"), app.app_context():
        raise ValueError("boom")
    assert _current_app_var.get() is None
