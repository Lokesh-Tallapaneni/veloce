"""Every optional-dependency error says the same thing the same way.

A user meeting one of these messages is blocked, and the message is the whole
of the help they get. Four spellings were in use - "install with:",
"Install with:", "Install it:", "Install it with:" - and the sentence shape
varied with them.

They now share one shape:

    <what needs it>. Install it with: pip install <target>

`<target>` is the **extra** where one exists (`veloceframework[metrics]`) and the
bare package where none does - `jinja2` and `ujson` have no extra, so naming one
would send the reader to an install that fails. That distinction is asserted
here against `pyproject.toml` rather than assumed, because it is the part that
would go wrong when a new extra is added and a message is not updated.
"""

from __future__ import annotations

import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "veloce"

# Messages, not prose: docstrings and module headers document install lines too,
# and those are not what a blocked user sees.
HINT = re.compile(r'"[^"]*Install[^"]*pip install ([^"\s]+)[^"]*"')


def _hint_lines() -> list[tuple[str, str]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "pip install" not in line or '"' not in line:
                continue
            match = HINT.search(line)
            if match:
                found.append((path.relative_to(SRC).as_posix(), line.strip()))
    return found


def _declared_extras() -> set[str]:
    text = (SRC.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split("[project.optional-dependencies]", 1)[1].split("\n[", 1)[0]
    return set(re.findall(r"^([a-z0-9_-]+) = \[", block, re.M))


def test_there_are_hints_to_check():
    """A regex that matched nothing would make every assertion below vacuous."""
    assert len(_hint_lines()) >= 5


@pytest.mark.parametrize("case", _hint_lines(), ids=lambda c: c[0])
def test_every_hint_uses_the_same_wording(case):
    _path, line = case
    assert "Install it with: pip install" in line, line


@pytest.mark.parametrize("case", _hint_lines(), ids=lambda c: c[0])
def test_a_hint_names_an_extra_that_exists(case):
    """An extra named in a message must be one `pip` can actually install."""
    _path, line = case
    match = re.search(r"pip install veloceframework\[([a-z0-9_-]+)\]", line)
    if match is None:
        return
    assert match.group(1) in _declared_extras(), line


def test_a_package_with_no_extra_is_named_directly():
    """`jinja2` and `ujson` have no extra, so the message must name the package -
    sending the reader to `veloceframework[templating]` would fail."""
    extras = _declared_extras()
    assert "templating" not in extras
    assert "ujson" not in extras

    lines = {path: line for path, line in _hint_lines()}
    assert "pip install jinja2" in lines["contrib/templating.py"]
    assert "pip install ujson" in lines["http/response.py"]


@pytest.mark.parametrize("extra", ["metrics", "otel", "gunicorn", "cli", "redis"])
def test_the_extra_backed_hints_name_their_extra(extra):
    joined = " ".join(line for _path, line in _hint_lines())
    assert f"pip install veloceframework[{extra}]" in joined


# ── the messages still reach a user ──────────────────────────────────


def test_a_missing_optional_dependency_raises_with_its_hint():
    """The negative: consistent wording is worthless if the message never
    surfaces. Checked through the real guard rather than by reading source."""
    import sys

    from veloce.http.response import UJSONResponse

    if "ujson" in sys.modules:
        pytest.skip("ujson is installed in this environment")
    with pytest.raises(ImportError) as excinfo:
        UJSONResponse({"a": 1})
    assert "Install it with: pip install ujson" in str(excinfo.value)
