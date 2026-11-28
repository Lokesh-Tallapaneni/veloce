"""Every guide page names the source and tests behind it, and the links are real.

Each page under `docs/guide/`, `docs/how-to/`, `docs/deployment/` and `docs/tutorial/` ends with a
`**Source and tests:**` line linking the modules that implement what it
describes and the tests that check it. A link to a file that has since moved or
been renamed sends a contributor nowhere, so every linked path must exist.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SECTIONS = ("guide", "how-to", "deployment", "tutorial")
PREFIX = "https://github.com/Lokesh-Tallapaneni/veloce/blob/main/"
LINE = re.compile(r"^\*\*Source and tests:\*\* (.+)$", re.M)
LINK = re.compile(r"\]\((" + re.escape(PREFIX) + r"[^)]+)\)")

PAGES = sorted(p for section in SECTIONS for p in (DOCS / section).glob("*.md"))
IDS = [p.relative_to(DOCS).as_posix() for p in PAGES]


def test_the_scan_finds_the_pages():
    assert len(PAGES) > 40


@pytest.mark.parametrize("page", PAGES, ids=IDS)
def test_the_page_ends_with_a_source_and_tests_line(page: pathlib.Path):
    text = page.read_text(encoding="utf-8").rstrip("\n")
    last = text.splitlines()[-1]
    assert LINE.match(last), f"{page.name} does not end with a `**Source and tests:**` line"


@pytest.mark.parametrize("page", PAGES, ids=IDS)
def test_every_linked_path_exists(page: pathlib.Path):
    match = LINE.search(page.read_text(encoding="utf-8"))
    assert match, page.name
    paths = [url[len(PREFIX) :] for url in LINK.findall(match.group(1))]
    assert paths, f"{page.name}: the line carries no links"
    assert any(p.startswith("src/") for p in paths), f"{page.name}: no source link"
    assert any(p.startswith("tests/") for p in paths), f"{page.name}: no test link"
    missing = [p for p in paths if not (ROOT / p).is_file()]
    assert missing == [], f"{page.name} links to files that do not exist: {missing}"
