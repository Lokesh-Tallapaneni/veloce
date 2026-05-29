"""End-to-end coverage for safe_join's case-insensitive descendant check.

Exercises the fix in `src/veloce/safe.py` that compares the joined path
against the base via `os.path.normcase`, so a Windows drive-letter or
filename casing difference doesn't reject an in-tree path.
"""

from __future__ import annotations

import os
import sys

import pytest

from veloce import Request
from veloce.contrib.staticfiles import StaticFiles
from veloce.safe import safe_join


def _req(path: str) -> Request:
    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


def test_safe_join_mixed_case_descendant(monkeypatch):
    """With case-folding forced, mixed-case base/joined still resolves."""
    monkeypatch.setattr(os.path, "normcase", str.lower)
    result = safe_join("/SRV/uploads", "Alice/file.txt")
    assert result is not None
    assert result.lower().endswith(os.path.join("uploads", "alice", "file.txt").lower())


def test_safe_join_rejects_parent_escape():
    assert safe_join("/srv/uploads", "../etc/passwd") is None


def test_safe_join_with_no_extra_components_returns_base():
    assert safe_join("/srv/uploads") == os.path.abspath("/srv/uploads")


def test_safe_join_rejects_absolute_component():
    assert safe_join("/srv/uploads", "/etc/passwd") is None


def test_safe_join_rejects_nul_byte():
    assert safe_join("/srv/uploads", "file\x00.txt") is None


def test_safe_join_same_directory_returns_base():
    """No-op join (single empty-relative component) stays inside the base."""
    result = safe_join("/srv/uploads", "")
    assert result == os.path.abspath("/srv/uploads")


def test_safe_join_sibling_prefix_collision_rejected(monkeypatch):
    """`/srv/a` must not accept `/srv/abc` even with case-folding active."""
    monkeypatch.setattr(os.path, "normcase", str.lower)
    base = os.path.abspath("/srv/a")
    sibling = os.path.abspath("/srv/abc")
    # Construct via abspath then re-derive the relative bit so the joined
    # path actually lands at the sibling directory.
    rel = os.path.relpath(sibling, base)
    assert safe_join(base, rel) is None


async def test_staticfiles_serves_mixed_case_subdirectory(tmp_path):
    """E2E: a request under a capitalised subdirectory resolves through safe_join."""
    served = tmp_path / "public"
    (served / "Alice").mkdir(parents=True)
    (served / "Alice" / "file.txt").write_bytes(b"hello alice")

    sf = StaticFiles(directory=str(served), prefix="/static")
    resp = await sf.handle(_req("/static/Alice/file.txt"))
    assert resp is not None
    assert resp.status_code == 200
    assert resp.body == b"hello alice"


async def test_staticfiles_rejects_parent_escape(tmp_path):
    """E2E: `..` segment is refused by safe_join before any filesystem touch."""
    served = tmp_path / "public"
    served.mkdir()
    (tmp_path / "secret.txt").write_bytes(b"top secret")

    sf = StaticFiles(directory=str(served), prefix="/static")
    resp = await sf.handle(_req("/static/../secret.txt"))
    assert resp is not None
    assert resp.status_code == 403


async def test_staticfiles_case_folded_base_serves_descendant(tmp_path, monkeypatch):
    """Force case-folded comparison so a casing-differing base still matches.

    On POSIX `normcase` is the identity, so the descendant check would
    pass trivially. Patching `normcase` to `str.lower` simulates the
    Windows path where the fix in safe.py is load-bearing — even with
    aggressive folding the served file is still reachable.
    """
    served = tmp_path / "Public"
    (served / "Bob").mkdir(parents=True)
    (served / "Bob" / "data.txt").write_bytes(b"hi bob")

    monkeypatch.setattr(os.path, "normcase", str.lower)
    sf = StaticFiles(directory=str(served), prefix="/static")
    resp = await sf.handle(_req("/static/Bob/data.txt"))
    assert resp is not None
    assert resp.status_code == 200
    assert resp.body == b"hi bob"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only drive-letter casing test")
async def test_staticfiles_windows_drive_letter_casing(tmp_path):
    """Windows: a base path whose drive letter differs in case from the
    joined path still resolves to the same descendant."""
    served = tmp_path / "public"
    (served / "Alice").mkdir(parents=True)
    (served / "Alice" / "file.txt").write_bytes(b"hello")

    # Flip the drive letter case on the served directory string.
    base = str(served)
    drive, rest = os.path.splitdrive(base)
    flipped = (drive.lower() if drive.isupper() else drive.upper()) + rest

    sf = StaticFiles(directory=flipped, prefix="/static")
    resp = await sf.handle(_req("/static/Alice/file.txt"))
    assert resp is not None
    assert resp.status_code == 200
    assert resp.body == b"hello"
