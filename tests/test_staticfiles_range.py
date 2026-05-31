"""StaticFiles HTTP Range support (ST4)."""

from __future__ import annotations

import pytest

from veloce import Request
from veloce.contrib.staticfiles import StaticFiles


def _req(path: str, headers: dict | None = None) -> Request:
    return Request(
        method="GET",
        path=path,
        query_string="",
        headers=headers or {},
        body=b"",
    )


@pytest.fixture()
def static(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"0123456789" * 10)  # 100 bytes
    return StaticFiles(directory=str(tmp_path), prefix="/static"), str(f)


# ── Plain GET emits Accept-Ranges ────────────────────────────────────


@pytest.mark.asyncio
async def test_plain_get_advertises_accept_ranges(static):
    sf, _ = static
    resp = await sf.handle(_req("/static/blob.bin"))
    assert resp.status_code == 200
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert len(resp.body) == 100


# ── Open-ended ranges ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_range_open_end(static):
    """`bytes=10-` returns bytes 10..end."""
    sf, _ = static
    resp = await sf.handle(_req("/static/blob.bin", {"range": "bytes=10-"}))
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 10-99/100"
    assert resp.body == b"0123456789" * 9


@pytest.mark.asyncio
async def test_range_closed(static):
    """`bytes=0-9` returns first 10 bytes inclusive."""
    sf, _ = static
    resp = await sf.handle(_req("/static/blob.bin", {"range": "bytes=0-9"}))
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 0-9/100"
    assert resp.body == b"0123456789"


@pytest.mark.asyncio
async def test_range_suffix(static):
    """`bytes=-20` returns last 20 bytes."""
    sf, _ = static
    resp = await sf.handle(_req("/static/blob.bin", {"range": "bytes=-20"}))
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 80-99/100"
    assert resp.body == (b"0123456789" * 10)[-20:]


@pytest.mark.asyncio
async def test_range_end_past_eof_clamped(static):
    """`bytes=90-1000` over a 100-byte file → 90-99/100, not 416."""
    sf, _ = static
    resp = await sf.handle(_req("/static/blob.bin", {"range": "bytes=90-1000"}))
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 90-99/100"
    assert len(resp.body) == 10


# ── 416 for fully unsatisfiable ──────────────────────────────────────


@pytest.mark.asyncio
async def test_range_start_past_eof_returns_416(static):
    sf, _ = static
    resp = await sf.handle(_req("/static/blob.bin", {"range": "bytes=200-300"}))
    assert resp.status_code == 416
    assert resp.headers["Content-Range"] == "bytes */100"


# ── Headers preserved alongside Content-Range ────────────────────────


@pytest.mark.asyncio
async def test_partial_response_keeps_etag_and_last_modified(static):
    sf, _ = static
    resp = await sf.handle(_req("/static/blob.bin", {"range": "bytes=0-9"}))
    assert resp.headers["ETag"].startswith('W/"')
    assert "Last-Modified" in resp.headers
    assert resp.headers["Accept-Ranges"] == "bytes"


# ── If-Range gate (RFC 9110 Sec. 13.1.5) ──────────────────────────────


@pytest.mark.asyncio
async def test_if_range_weak_etag_serves_full_200(static):
    """RFC 9110 Sec. 13.1.5 mandates STRONG comparison for an If-Range ETag.
    Veloce emits weak file ETags, so even a byte-identical weak ETag cannot
    authorize a range resume - the server returns the full 200."""
    sf, _ = static
    etag = (await sf.handle(_req("/static/blob.bin"))).headers["ETag"]
    assert etag.startswith("W/")  # the server's file ETags are weak
    resp = await sf.handle(_req("/static/blob.bin", {"Range": "bytes=0-9", "If-Range": etag}))
    assert resp.status_code == 200
    assert len(resp.body) == 100
    assert "Content-Range" not in resp.headers


@pytest.mark.asyncio
async def test_if_range_exact_date_serves_206(static):
    """An If-Range HTTP-date that exactly matches the file's Last-Modified
    authorizes the range resume (RFC 9110 Sec. 13.1.5 exact-match rule)."""
    sf, _ = static
    last_modified = (await sf.handle(_req("/static/blob.bin"))).headers["Last-Modified"]
    resp = await sf.handle(
        _req("/static/blob.bin", {"Range": "bytes=0-9", "If-Range": last_modified})
    )
    assert resp.status_code == 206
    assert len(resp.body) == 10


@pytest.mark.asyncio
async def test_if_range_stale_etag_serves_full_200(static):
    """A Range with a stale If-Range ETag downgrades to a full 200, not a 206 slice."""
    sf, _ = static
    resp = await sf.handle(
        _req("/static/blob.bin", {"Range": "bytes=0-9", "If-Range": '"stale-different-version"'})
    )
    assert resp.status_code == 200
    assert len(resp.body) == 100
    assert "Content-Range" not in resp.headers


@pytest.mark.asyncio
async def test_if_range_stale_date_serves_full_200(static):
    """An If-Range HTTP-date older than the file's mtime downgrades to a full 200."""
    sf, _ = static
    resp = await sf.handle(
        _req(
            "/static/blob.bin",
            {"Range": "bytes=0-9", "If-Range": "Wed, 01 Jan 2020 00:00:00 GMT"},
        )
    )
    assert resp.status_code == 200
    assert len(resp.body) == 100
