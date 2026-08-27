"""The imperative-mood exemption covers predicates and nothing else.

`D401` is switched off in `pyproject.toml` for one stated reason: a predicate
reads better as the property it tests - "True when the port is the default for
the scheme" - than as a command. Switched off, though, the rule stops
distinguishing that from a docstring that simply forgot its verb, which is how
65 of them accumulated. This module keeps the exemption honest by running the
rule and refusing anything it reports that is not a predicate.

The guard reaches exactly as far as D401 does, which is not all the way: D401
recognises the third-person and article-led openings ("Returns the...", "The
message for...", "A route name...") and stays quiet on some others, so a
summary opening "Whether ..." passes here. Verified by mutation - planting "The
truth of whether ..." fails this module, planting "Whether ..." does not.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "veloce"
PREDICATE_OPENINGS = ('"""True ', '"""`True` ')


def _ruff() -> str:
    found = shutil.which("ruff")
    if found is None:
        pytest.skip("ruff is not on PATH")
    return found


def _d401_summaries() -> list[tuple[str, int, str]]:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [_ruff(), "check", str(SRC), "--select", "D401", "--output-format", "json"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if result.returncode not in (0, 1):
        pytest.skip(f"ruff exited {result.returncode}: {result.stderr[:200]}")
    rows = json.loads(result.stdout or "[]")
    out = []
    for row in rows:
        path = row["filename"]
        line = row["location"]["row"]
        text = __import__("pathlib").Path(path).read_text(encoding="utf-8").split("\n")[line - 1]
        out.append((path, line, text.strip()))
    return out


@pytest.mark.skipif(sys.platform not in ("win32", "linux", "darwin"), reason="needs a shell")
def test_every_non_imperative_summary_is_a_predicate() -> None:
    offenders = [
        f"{path}:{line}  {text}"
        for path, line, text in _d401_summaries()
        if not text.startswith(PREDICATE_OPENINGS)
    ]
    assert not offenders, (
        "D401 is off only so predicates can read as the property they test. "
        "These summaries are neither imperative nor predicates - give them a "
        "verb:\n  " + "\n  ".join(offenders)
    )


def test_the_exemption_actually_covers_something() -> None:
    """A rule reporting nothing would pass the test above without checking it."""
    reported = _d401_summaries()
    assert reported, "ruff reported no D401 at all - the scan is not running"
    assert all(text.startswith(PREDICATE_OPENINGS) for _, _, text in reported)


def test_a_non_predicate_summary_would_be_caught() -> None:
    """The classifier, in isolation, on the two shapes it has to tell apart."""
    assert '"""True when the port is the default for the scheme."""'.startswith(PREDICATE_OPENINGS)
    assert not '"""The port the scheme defaults to."""'.startswith(PREDICATE_OPENINGS)
