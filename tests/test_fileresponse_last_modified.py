"""FileResponse emits Last-Modified (Q40 partial)."""

from __future__ import annotations

import os
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

import pytest

from veloce import FileResponse


@pytest.fixture
def fixture_file(tmp_path: Path) -> tuple[Path, int]:
    p = tmp_path / "data.bin"
    p.write_bytes(b"hello")
    mtime = 1_700_000_000
    os.utime(str(p), (mtime, mtime))
    return p, mtime


def test_fileresponse_emits_last_modified_header(fixture_file):
    path, mtime = fixture_file
    resp = FileResponse(str(path))
    assert "Last-Modified" in resp.headers
    assert resp.headers["Last-Modified"] == formatdate(mtime, usegmt=True)


def test_fileresponse_last_modified_is_imf_fixdate(fixture_file):
    """The header value must be a parseable HTTP-date per RFC 9110."""
    path, mtime = fixture_file
    resp = FileResponse(str(path))
    parsed = parsedate_to_datetime(resp.headers["Last-Modified"])
    assert parsed.timestamp() == mtime


def test_caller_supplied_last_modified_wins(fixture_file):
    """If the caller passes their own Last-Modified, it isn't overridden."""
    path, _ = fixture_file
    resp = FileResponse(str(path), headers={"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"})
    assert resp.headers["Last-Modified"] == "Mon, 01 Jan 2024 00:00:00 GMT"


@pytest.mark.asyncio
async def test_async_from_path_emits_last_modified(fixture_file):
    path, mtime = fixture_file
    resp = await FileResponse.from_path(str(path))
    assert resp.headers["Last-Modified"] == formatdate(mtime, usegmt=True)


@pytest.mark.asyncio
async def test_async_from_path_caller_last_modified_wins(fixture_file):
    path, _ = fixture_file
    resp = await FileResponse.from_path(
        str(path),
        headers={"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"},
    )
    assert resp.headers["Last-Modified"] == "Mon, 01 Jan 2024 00:00:00 GMT"
