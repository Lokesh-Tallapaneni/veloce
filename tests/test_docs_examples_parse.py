"""Every Python example in `docs/` is valid Python.

`CLAUDE.md` requires documentation examples to be copy-paste runnable, and 574
of them were ungated - one had already rotted into a fragment that does not
parse. Parsing is the cheap half of that promise and catches the whole class of
breakage that editing prose around code produces.

This deliberately does not execute the examples: many need a running server, a
database or a browser. It asserts they are syntactically Python, which is the
part that can be checked for all of them at once.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

_DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"

#: A fenced block whose info string starts with `python`. `title=` and
#: `hl_lines=` attributes are part of the info string, not the code.
_BLOCK = re.compile(r"^```python[^\n]*\n(.*?)^```", re.S | re.M)

#: Info strings marking a block that is deliberately not a whole program.
_SKIP_MARKERS = ("no-parse", "fragment")


def _python_blocks() -> list[tuple[str, int, str]]:
    """Return `(page, line, source)` for every Python block under `docs/`."""
    found: list[tuple[str, int, str]] = []
    for page in sorted(_DOCS.rglob("*.md")):
        text = page.read_text(encoding="utf-8")
        for match in _BLOCK.finditer(text):
            info_line = text[: match.start()].count("\n") + 1
            info = text[match.start() : text.index("\n", match.start())]
            if any(marker in info for marker in _SKIP_MARKERS):
                continue
            found.append((str(page.relative_to(_DOCS)), info_line, match.group(1)))
    return found


_BLOCKS = _python_blocks()


def test_the_docs_tree_actually_has_examples():
    """A regex that silently matches nothing would make this suite vacuous."""
    assert len(_BLOCKS) > 100, f"only found {len(_BLOCKS)} python blocks"


@pytest.mark.parametrize(
    ("page", "line", "source"),
    _BLOCKS,
    ids=[f"{page}:{line}" for page, line, _ in _BLOCKS],
)
def test_a_documented_example_is_valid_python(page: str, line: int, source: str):
    try:
        ast.parse(source)
    except SyntaxError as exc:
        offending = source.splitlines()[max(0, (exc.lineno or 1) - 1)]
        pytest.fail(
            f"{page} line {line}: {exc.msg}\n"
            f"  offending line: {offending.rstrip()}\n"
            f"  mark the block ```python no-parse``` if it is deliberately a fragment"
        )
