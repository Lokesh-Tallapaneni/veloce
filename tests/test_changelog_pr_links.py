"""Every changelog entry's PR label agrees with the URL beside it.

An entry ends with `([#NNN](https://github.com/.../pull/NNN))`, and the number
appears twice. A reader sees the label; the browser follows the URL. When the
two disagree the entry looks right in every rendering of the file and takes
anyone who clicks it somewhere else.

That is not hypothetical: 0.18.0's entries were written against a guessed pull
request number, and the correction replaced the URL and left the label, so all
295 entries then read `[#288]` while pointing at 289.

Whether the URL names the *right* pull request needs GitHub, so the release
workflow checks that separately - it refuses to publish while the notes for the
version being tagged link a pull request that is not merged. This half needs
nothing but the file, so it runs on every change instead of only on a release.
"""

from __future__ import annotations

import pathlib
import re

CHANGELOG = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"

#: A markdown link whose label is a `#NNN` pull-request reference. Both numbers
#: are captured so they can be compared.
_LINK = re.compile(r"\[#(\d+)\]\((https?://[^)]*?/pull/(\d+))\)")

#: An entry's trailing reference, however it was written - used only to count,
#: so the checks below cannot pass by matching nothing.
_ANY_PR_URL = re.compile(r"/pull/(\d+)")


def _text() -> str:
    return CHANGELOG.read_text(encoding="utf-8")


def test_the_changelog_carries_pr_links_to_check():
    """The floor: a scan of nothing would satisfy every assertion below."""
    assert len(_ANY_PR_URL.findall(_text())) >= 50


def test_every_pr_label_matches_its_url():
    """The regression: `[#288]` pointing at `/pull/289`."""
    mismatched = [
        f"[#{m.group(1)}] links {m.group(2)}"
        for m in _LINK.finditer(_text())
        if m.group(1) != m.group(3)
    ]

    assert not mismatched, (
        "a changelog entry names one pull request and links another, so the text "
        f"and the link disagree: {mismatched[:5]}"
    )


def test_every_pr_reference_is_a_link():
    """A bare `(#123)` renders as text, so the reader cannot reach the PR."""
    bare = [
        line.strip()[:90]
        for line in _text().splitlines()
        if re.search(r"\(#\d+\)\s*$", line) and "](" not in line
    ]

    assert not bare, f"these entries name a pull request without linking it: {bare[:5]}"


def test_the_check_would_catch_a_mismatch():
    """The mutation, inline: the pattern must actually report a disagreement."""
    sample = "- Something. ([#288](https://github.com/o/r/pull/289))"
    found = [m for m in _LINK.finditer(sample) if m.group(1) != m.group(3)]

    assert found, "the pattern does not detect a label that disagrees with its URL"


def test_the_check_accepts_an_agreeing_link():
    """The other half: a correct entry must not be reported."""
    sample = "- Something. ([#289](https://github.com/o/r/pull/289))"
    found = [m for m in _LINK.finditer(sample) if m.group(1) != m.group(3)]

    assert not found
