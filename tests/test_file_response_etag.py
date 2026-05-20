"""FileResponse ETag header emission (Q42)."""

from __future__ import annotations

import re

import pytest

from veloce import FileResponse

_ETAG_RE = re.compile(r'^"[0-9a-f]{32}"$')


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
    # Touch the size so mtime changes and content differs.
    import time

    time.sleep(0.01)
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


@pytest.mark.asyncio
async def test_async_emits_etag(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    resp = await FileResponse.from_path(str(p))
    assert _ETAG_RE.match(resp.headers["ETag"])


@pytest.mark.asyncio
async def test_async_caller_supplied_etag_wins(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"x")
    resp = await FileResponse.from_path(str(p), headers={"ETag": '"custom"'})
    assert resp.headers["ETag"] == '"custom"'


@pytest.mark.asyncio
async def test_async_etag_matches_sync_for_same_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    sync_e = FileResponse(str(p)).headers["ETag"]
    async_e = (await FileResponse.from_path(str(p))).headers["ETag"]
    assert sync_e == async_e


# ── Coexists with Last-Modified ──────────────────────────────────────


def test_etag_and_last_modified_both_present(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"x")
    resp = FileResponse(str(p))
    assert "ETag" in resp.headers
    assert "Last-Modified" in resp.headers
