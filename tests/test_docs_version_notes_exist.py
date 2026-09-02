"""Every version a docs admonition names exists as a changelog heading.

`docs/guide/dependency-injection.md` told readers a behaviour changed "in
version 0.19.1" while the changelog had no such heading and the entries sat
under Unreleased. A reader following that pointer to the release notes found
nothing, and neither the docs build nor the test suite noticed.

The check is a grep that runs on every commit rather than one somebody
remembers to do before a release. A cited version resolves when the changelog
carries a heading beginning with it, so both the `0.18` and `0.18.0` spellings
the docs already use match `## [0.18.0]`.
"""

from __future__ import annotations

import pathlib
import re

#: The admonition the docs use to date a behaviour, e.g.
#: `!!! note "Changed in version 0.20.0"`. Both `Added` and `Changed` are dated
#: this way, and the version is the trailing dotted number.
_NOTE = re.compile(r'!!!\s+note\s+"(?:Added|Changed) in version ([0-9]+(?:\.[0-9]+)*)"')

#: A released section, e.g. `## [0.18.0] - 2026-08-31`. `## [Unreleased]` is
#: deliberately not matched: a docs note pointing at it would send a reader to
#: notes that carry no version at all.
_HEADING = re.compile(r"^## \[([0-9]+\.[0-9]+\.[0-9]+)\]", re.MULTILINE)


def _root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _versions_in_changelog() -> set[str]:
    text = (_root() / "CHANGELOG.md").read_text(encoding="utf-8")
    released = _HEADING.findall(text)
    # `0.18` must resolve against `## [0.18.0]`, so every truncation of a
    # released version counts as naming it.
    known = set(released)
    for version in released:
        parts = version.split(".")
        known.update(".".join(parts[:count]) for count in range(1, len(parts)))
    return known


def _notes_in_docs() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    root = _root()
    for path in sorted((root / "docs").rglob("*.md")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = _NOTE.search(line)
            if match:
                found.append((path.relative_to(root).as_posix(), number, match.group(1)))
    return found


def test_the_scan_finds_version_notes_to_check():
    """The guard is worthless if the pattern matches nothing."""
    assert len(_notes_in_docs()) > 5


def test_every_documented_version_exists_in_the_changelog():
    """NEGATIVE: a docs note must not cite a release that was never cut."""
    known = _versions_in_changelog()
    bad = [(page, line, cited) for page, line, cited in _notes_in_docs() if cited not in known]
    assert not bad, f"docs cite versions absent from CHANGELOG.md: {bad}"


def test_the_check_would_catch_a_fabricated_version():
    """POSITIVE control: a version that was never released is not accepted."""
    assert "0.99.7" not in _versions_in_changelog()


def test_a_released_version_is_recognised():
    """POSITIVE control: the comparison accepts a heading that does exist."""
    known = _versions_in_changelog()
    assert "0.19.0" in known
    assert "0.19" in known
