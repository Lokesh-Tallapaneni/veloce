"""Late imports in the suite must be guarded, not habitual.

A module whose import block restarts halfway down reads as two eras of tests
stacked on top of each other, and the `# noqa: E402` that makes it pass is the
tell. There is one legitimate reason to import below the header - the import
would raise unless a `pytest.importorskip` above it has already run - so that is
what this module checks for: every suppression, and every one only, sits under a
skip guard.
"""

from __future__ import annotations

import pathlib

import pytest

TESTS = pathlib.Path(__file__).resolve().parent
SUPPRESSION = "# noqa: E402"
GUARDS = ("importorskip", "pytest.skip", "skip_module")


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in TESTS.glob("test_*.py") if p.name != pathlib.Path(__file__).name)


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_every_late_import_sits_under_a_skip_guard(path: pathlib.Path) -> None:
    lines = path.read_text(encoding="utf-8").split("\n")
    for number, line in enumerate(lines, start=1):
        if not line.startswith(("import ", "from ")) or SUPPRESSION not in line:
            continue
        preceding = "\n".join(lines[: number - 1])
        assert any(guard in preceding for guard in GUARDS), (
            f"{path.name}:{number} imports below the module header behind "
            f"{SUPPRESSION!r} with no skip guard above it. Move it into the "
            "header block; the suppression is only for an import that cannot "
            "run until an importorskip has passed."
        )


def test_the_scan_recognises_an_unguarded_late_import() -> None:
    """The check above passes trivially if the scan never matches anything."""
    module = TESTS / "test_deferred_import_discipline.py"
    planted = ["x = 1", f"import json  {SUPPRESSION}"]
    offenders = [
        number
        for number, line in enumerate(planted, start=1)
        if line.startswith(("import ", "from ")) and SUPPRESSION in line
    ]
    assert offenders == [2]
    assert not any(guard in planted[0] for guard in GUARDS)
    assert module.exists()
