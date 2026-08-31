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

TESTS = pathlib.Path(__file__).resolve().parent
SUPPRESSION = "# noqa: E402"
GUARDS = ("importorskip", "pytest.skip", "skip_module")


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in TESTS.rglob("test_*.py") if p.name != pathlib.Path(__file__).name)


def _is_suppressed_import(line: str) -> bool:
    """Whether `line` is an import carrying the suppression comment.

    One definition, used by the corpus scan and by the guard that proves
    the scan is not vacuous. Written twice, narrowing one left the other
    reporting a live scanner that had stopped matching anything.
    """
    return line.startswith(("import ", "from ")) and SUPPRESSION in line


def test_the_late_import_scan_reads_a_real_corpus():
    """A scan of nothing passes every check below it.

    The glob is non-recursive and hard-codes the flat layout, so moving the
    test tree into subdirectories would leave this module green while reading
    no files at all.
    """
    assert len(_modules()) > 100, (
        "the module glob matched almost nothing, so the late-import scan reads nothing"
    )


def test_every_late_import_sits_under_a_skip_guard() -> None:
    """One scan of the corpus; the message names every offender."""
    offenders: list[str] = []
    for path in _modules():
        lines = path.read_text(encoding="utf-8").split("\n")
        for number, line in enumerate(lines, start=1):
            if not _is_suppressed_import(line):
                continue
            preceding = "\n".join(lines[: number - 1])
            if not any(guard in preceding for guard in GUARDS):
                offenders.append(f"{path.name}:{number}")
    assert offenders == [], (
        f"these import below the module header behind {SUPPRESSION!r} with no "
        "skip guard above them. Move them into the header block; the "
        "suppression is only for an import that cannot run until an "
        f"importorskip has passed: {offenders}"
    )


def test_the_scan_recognises_an_unguarded_late_import() -> None:
    """The check above passes trivially if the scan never matches anything."""
    planted = ["x = 1", f"import json  {SUPPRESSION}"]
    offenders = [
        number for number, line in enumerate(planted, start=1) if _is_suppressed_import(line)
    ]
    assert offenders == [2]
    assert not any(guard in planted[0] for guard in GUARDS)
