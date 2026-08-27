"""FileResponse ETag header emission (Q42)."""

from __future__ import annotations

import re

from veloce import FileResponse

_ETAG_RE = re.compile(r'^W/"[0-9a-f]{32}"$')


# ── Sync ctor ─────────────────────────────────────────────────────────


def test_sync_emits_etag(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    resp = FileResponse(str(p))
    assert _ETAG_RE.match(resp.headers["ETag"])


def test_sync_etag_stable_for_same_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    a = FileResponse(str(p)).headers["ETag"]
    b = FileResponse(str(p)).headers["ETag"]
    assert a == b


def test_sync_etag_changes_when_content_changes(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    e1 = FileResponse(str(p)).headers["ETag"]
    # The rewrite changes the file's *size*, which the ETag covers, so the two
    # differ whether or not the mtime ticks. A real `time.sleep(0.01)` used to
    # sit here forcing an mtime change the assertion never needed.
    p.write_bytes(b"hello world")
    e2 = FileResponse(str(p)).headers["ETag"]
    assert e1 != e2


def test_sync_caller_supplied_etag_wins(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"x")
    resp = FileResponse(str(p), headers={"ETag": '"custom"'})
    assert resp.headers["ETag"] == '"custom"'


def test_sync_caller_supplied_lowercase_etag_wins(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"x")
    resp = FileResponse(str(p), headers={"etag": '"lc"'})
    assert resp.headers["etag"] == '"lc"'
    assert "ETag" not in resp.headers


# ── Async factory ─────────────────────────────────────────────────────


async def test_async_emits_etag(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    resp = await FileResponse.from_path(str(p))
    assert _ETAG_RE.match(resp.headers["ETag"])


async def test_async_caller_supplied_etag_wins(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"x")
    resp = await FileResponse.from_path(str(p), headers={"ETag": '"custom"'})
    assert resp.headers["ETag"] == '"custom"'


async def test_async_etag_matches_sync_for_same_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    # The sync `FileResponse(path)` constructor emits a
    # `DeprecationWarning` on a running loop (it still does blocking I/O,
    # just no longer raises). Silence the warning here — the comparison
    # is the test's actual concern, not the deprecation surface.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        sync_e = FileResponse(str(p)).headers["ETag"]
    async_e = (await FileResponse.from_path(str(p))).headers["ETag"]
    assert sync_e == async_e


def test_file_etag_passes_usedforsecurity_false(tmp_path, monkeypatch):
    """The file ETag MD5 must be flagged non-security so it is FIPS-safe."""
    import hashlib

    seen = {}
    real_md5 = hashlib.md5

    def _spy(data=b"", *, usedforsecurity=True):
        seen["flag"] = usedforsecurity
        return real_md5(data, usedforsecurity=usedforsecurity)

    monkeypatch.setattr(hashlib, "md5", _spy)
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    resp = FileResponse(str(p))
    assert resp.headers["ETag"].startswith('W/"')
    assert seen["flag"] is False


# ── Coexists with Last-Modified ──────────────────────────────────────


def test_etag_and_last_modified_both_present(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"x")
    resp = FileResponse(str(p))
    assert "ETag" in resp.headers
    assert "Last-Modified" in resp.headers


# ── Weak validator (RFC 9110 §8.8.3) ─────────────────────────────────


def test_file_etag_is_weak(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    resp = FileResponse(str(p))
    # mtime-derived tags can collide within the same second across
    # content-altering writes, so they must be marked weak.
    assert resp.headers["ETag"].startswith('W/"')
