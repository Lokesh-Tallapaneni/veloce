"""`tests/` holds only the cross-cutting guards; everything else mirrors the package.

A test for one module lives in the folder named for it (`tests/http/`,
`tests/mcp/`, ...). What stays at the top level are the files that scan the whole
repository rather than exercise one module: import-graph and naming guards,
documentation-claim checks, the public-surface and export-parity checks. They
are named in `tests/top_level_tests.txt`, which the `forbid-new-top-level-tests`
pre-commit hook reads, so a new file cannot land here by accident.

This keeps the list honest in the other direction: an entry whose file moved or
was deleted fails here instead of leaving the hook permissive for a name nothing
uses.
"""

from __future__ import annotations

import pathlib

TESTS = pathlib.Path(__file__).resolve().parent
LISTED = (TESTS / "top_level_tests.txt").read_text(encoding="utf-8").split()


def test_every_listed_file_exists_at_the_top_level():
    missing = [name for name in LISTED if not (TESTS / name).is_file()]
    assert missing == [], f"listed in top_level_tests.txt but not in tests/: {missing}"


def test_every_top_level_test_is_listed():
    unlisted = sorted(p.name for p in TESTS.glob("test_*.py") if p.name not in LISTED)
    assert unlisted == [], (
        f"top-level test files not in top_level_tests.txt: {unlisted}. Put a test "
        "for one module in the subfolder that mirrors it."
    )


def test_the_list_has_no_duplicates():
    assert len(LISTED) == len(set(LISTED))
