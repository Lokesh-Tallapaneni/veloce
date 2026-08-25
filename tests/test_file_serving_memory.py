"""Serving a large file does not hold it in memory.

A standing report had this open: "`FileResponse` reads whole files into memory …
four concurrent 32 MiB downloads at **134 MB RSS**". Measured against the current
tree, it is not true — both async paths stream, and 4 x 32 MiB concurrent peaks
under 1 MB. The streaming was added and the report was never re-run.

Nothing in the suite proved it, which is exactly how a fixed thing stays listed
as broken for months. These tests are that proof, and they are written as memory
assertions rather than as "does it return the right bytes", because the bytes
were always right — the resident memory was the defect.

What deliberately still buffers:

* `FileResponse(path)`, the **sync** constructor. A synchronous API cannot await
  an executor, so it reads the file whole. It warns when called on a running loop
  and names `from_path` as the async form. That is a signposted limit, not a bug.
* Files below each threshold. One ASGI message beats N for a small asset, and the
  per-chunk executor hop dominates at that size.
* A range response, which is already bounded by what the client asked for.
"""

from __future__ import annotations

import asyncio
import tracemalloc
import warnings

import pytest

from veloce import Veloce
from veloce.contrib.staticfiles import StaticFiles
from veloce.http.response import FileResponse

#: Comfortably over both streaming thresholds (`FileResponse` 64 KiB,
#: `StaticFiles` 1 MiB) without making the suite slow.
BIG = 4 * 1024 * 1024
SMALL = 1024


@pytest.fixture
def files(tmp_path):
    """A big file and a small one, in a directory `StaticFiles` can mount."""
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "big.bin").write_bytes(b"x" * BIG)
    (assets / "small.bin").write_bytes(b"y" * SMALL)
    return assets


def _scope(path: str, headers: list | None = None) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
        "scheme": "http",
        "root_path": "",
    }


async def _drain(app, path: str, headers: list | None = None) -> dict:
    """Drive one request, counting body messages without keeping them."""
    stats = {"messages": 0, "bytes": 0, "biggest": 0, "status": None, "headers": {}}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            stats["status"] = message["status"]
            stats["headers"] = {k.decode().lower(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if chunk:
                stats["messages"] += 1
                stats["bytes"] += len(chunk)
                stats["biggest"] = max(stats["biggest"], len(chunk))
            await asyncio.sleep(0)

    await app(_scope(path, headers), receive, send)
    return stats


async def _peak_mb(coro_factory) -> tuple[float, object]:
    """Peak traced allocation, in MB, while awaiting `coro_factory()`."""
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    result = await coro_factory()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return (peak - base) / 1e6, result


def _static_app(files) -> Veloce:
    app = Veloce(openapi_url=None)
    app.mount("/assets", StaticFiles(directory=str(files)))
    return app


def _download_app(files) -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/big")
    async def big():
        return await FileResponse.from_path(str(files / "big.bin"))

    @app.get("/small")
    async def small():
        return await FileResponse.from_path(str(files / "small.bin"))

    return app


# ── FileResponse.from_path streams ───────────────────────────────────


async def test_from_path_does_not_hold_the_file(files):
    """The reported defect, measured: it holds one chunk, not the file."""
    response = await FileResponse.from_path(str(files / "big.bin"))
    assert response.is_streamed is True
    assert response.body == b""


async def test_from_path_keeps_the_length_known(files):
    """Streamed, but length-delimited - not switched to chunked encoding."""
    response = await FileResponse.from_path(str(files / "big.bin"))
    assert int(response.headers["Content-Length"]) == BIG


async def test_a_streamed_download_stays_bounded(files):
    peak, stats = await _peak_mb(lambda: _drain(_download_app(files), "/big"))
    assert stats["bytes"] == BIG
    assert peak < 1.0, f"peaked at {peak:.2f} MB serving a {BIG / 1e6:.0f} MB file"


async def test_a_streamed_download_arrives_in_chunks(files):
    stats = await _drain(_download_app(files), "/big")
    assert stats["messages"] > 1
    assert stats["biggest"] <= 128 * 1024


async def test_a_streamed_download_delivers_every_byte(files):
    stats = await _drain(_download_app(files), "/big")
    assert stats["status"] == 200
    assert stats["bytes"] == BIG


# ── StaticFiles streams ──────────────────────────────────────────────


async def test_static_files_stays_bounded(files):
    peak, stats = await _peak_mb(lambda: _drain(_static_app(files), "/assets/big.bin"))
    assert stats["bytes"] == BIG
    assert peak < 1.0, f"peaked at {peak:.2f} MB serving a {BIG / 1e6:.0f} MB file"


async def test_static_files_arrives_in_chunks(files):
    stats = await _drain(_static_app(files), "/assets/big.bin")
    assert stats["messages"] > 1
    assert stats["biggest"] <= 128 * 1024


async def test_static_files_delivers_every_byte(files):
    stats = await _drain(_static_app(files), "/assets/big.bin")
    assert stats["status"] == 200
    assert stats["bytes"] == BIG


# ── concurrency is the case that mattered ────────────────────────────


@pytest.mark.parametrize("factory", ["static", "download"])
async def test_concurrent_downloads_do_not_multiply_memory(files, factory):
    """The report's headline: "four concurrent 32 MiB downloads at 134 MB RSS"."""
    app = _static_app(files) if factory == "static" else _download_app(files)
    path = "/assets/big.bin" if factory == "static" else "/big"
    await _drain(app, path)  # warm

    async def four():
        return await asyncio.gather(*(_drain(app, path) for _ in range(4)))

    peak, results = await _peak_mb(four)
    assert [r["bytes"] for r in results] == [BIG] * 4
    assert peak < 2.0, f"four concurrent downloads peaked at {peak:.2f} MB"


# ── what deliberately still buffers ──────────────────────────────────


async def test_a_small_file_is_not_streamed(files):
    """One ASGI message beats N below the threshold; the hop dominates."""
    response = await FileResponse.from_path(str(files / "small.bin"))
    assert response.is_streamed is False
    assert len(response.body) == SMALL


async def test_a_small_file_arrives_in_one_message(files):
    stats = await _drain(_download_app(files), "/small")
    assert stats["messages"] == 1
    assert stats["bytes"] == SMALL


async def test_static_files_buffers_a_small_file(files):
    stats = await _drain(_static_app(files), "/assets/small.bin")
    assert stats["messages"] == 1
    assert stats["bytes"] == SMALL


def test_the_sync_constructor_still_buffers(files):
    """A synchronous API cannot await an executor; this is a signposted limit."""
    response = FileResponse(str(files / "big.bin"))
    assert response.is_streamed is False
    assert len(response.body) == BIG


async def test_the_sync_constructor_warns_on_a_running_loop(files):
    """The signpost itself - it names the async form to use instead."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        FileResponse(str(files / "big.bin"))
    messages = [str(w.message) for w in caught]
    assert any("from_path" in m for m in messages), messages


async def test_a_range_request_buffers_its_slice(files):
    """A range is already bounded by what the client asked for."""
    stats = await _drain(
        _static_app(files), "/assets/big.bin", headers=[(b"range", b"bytes=0-1023")]
    )
    assert stats["status"] == 206
    assert stats["bytes"] == 1024
    assert stats["messages"] == 1


# ── the thresholds are where they say they are ───────────────────────


def test_the_thresholds_are_declared():
    from veloce.http.response import _INLINE_READ_MAX

    assert _INLINE_READ_MAX == 64 * 1024
    assert StaticFiles.STREAM_THRESHOLD == 1024 * 1024
    assert StaticFiles.STREAM_CHUNK_SIZE == 64 * 1024


async def test_a_file_just_over_the_threshold_streams(tmp_path):
    path = tmp_path / "just-over.bin"
    path.write_bytes(b"z" * (64 * 1024 + 1))
    assert (await FileResponse.from_path(str(path))).is_streamed is True


async def test_a_file_exactly_at_the_threshold_does_not_stream(tmp_path):
    """The boundary is inclusive on the buffered side; state it."""
    path = tmp_path / "exact.bin"
    path.write_bytes(b"z" * (64 * 1024))
    assert (await FileResponse.from_path(str(path))).is_streamed is False


# ── the async helpers route through the streaming path ───────────────


async def test_async_send_file_streams_a_large_file(files):
    from veloce.helpers import async_send_file

    response = await async_send_file(str(files / "big.bin"))
    assert response.is_streamed is True


async def test_async_send_file_buffers_a_small_file(files):
    from veloce.helpers import async_send_file

    response = await async_send_file(str(files / "small.bin"))
    assert response.is_streamed is False
