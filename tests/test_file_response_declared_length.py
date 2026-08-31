"""A streamed file sends exactly the `Content-Length` it declared.

`FileResponse.from_path` stats the file, declares `Content-Length: st.st_size`,
then hands `_stream_file` the path - which reads the open handle to EOF. Those
are two different moments. A file appended to in between (a log, an asset
replaced by a deploy) yields more bytes than the head promised.

On the native transport the surplus lands on a keep-alive connection after the
response the client was counting, so the client reads it as the beginning of the
next response. Under ASGI the server truncates or errors instead.

The declared length is the contract, so the read stops there.

The other direction - a file that *shrank* - still under-delivers, and is not
addressed here: it leaves the client waiting rather than handing it bytes from
one response as another, and fixing it means either padding invented bytes or
tearing down the connection.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from veloce.http.response import _INLINE_READ_MAX, FileResponse, _stream_file

#: Comfortably past the inline-read threshold, so the response is streamed.
SIZE = _INLINE_READ_MAX * 2


class _Transport:
    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data


def _append(path: pathlib.Path, count: int) -> None:
    """Grow the file, as a deploy or a log write would between stat and read."""
    path.write_bytes(path.read_bytes() + b"L" * count)


def _declared(head: bytes) -> int:
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            return int(line.split(b":", 1)[1])
    raise AssertionError("no Content-Length in the head")


async def _sent(path: pathlib.Path, grow_by: int = 0) -> tuple[int, int]:
    """Build the response, optionally grow the file, then stream it.

    Returns `(declared, body_bytes)`. Growing between the two is the race the
    defect turns into surplus bytes on the wire.
    """
    response = await FileResponse.from_path(str(path))
    if grow_by:
        _append(path, grow_by)

    transport = _Transport()
    await response.stream_to(transport, keep_alive=True)

    head, _, body = transport.written.partition(b"\r\n\r\n")
    return _declared(head + b"\r\n"), len(body)


@pytest.fixture
def asset(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "asset.bin"
    path.write_bytes(b"A" * SIZE)
    return path


async def test_an_unchanged_file_sends_what_it_declared(asset: pathlib.Path):
    """The control: the ordinary case must be exact, not merely capped."""
    declared, sent = await _sent(asset)

    assert declared == SIZE
    assert sent == declared


async def test_a_file_appended_to_mid_flight_sends_no_surplus(asset: pathlib.Path):
    """The regression: the extra bytes reached the client as the next response."""
    declared, sent = await _sent(asset, grow_by=4096)

    assert declared == SIZE
    assert sent == declared, f"{sent - declared} bytes beyond the declared length"


@pytest.mark.parametrize("grow_by", [1, 4096, SIZE], ids=["one", "chunk", "double"])
async def test_no_growth_size_produces_surplus(asset: pathlib.Path, grow_by: int):
    """A partial chunk overrunning the limit is the boundary worth naming."""
    declared, sent = await _sent(asset, grow_by=grow_by)

    assert sent == declared


async def test_the_body_is_the_files_original_content(asset: pathlib.Path):
    """Capping must take the first N bytes, not an arbitrary N."""
    response = await FileResponse.from_path(str(asset))
    _append(asset, 4096)

    transport = _Transport()
    await response.stream_to(transport, keep_alive=True)
    _head, _, body = transport.written.partition(b"\r\n\r\n")

    assert body == b"A" * SIZE
    assert b"L" not in body


# ── the helper, directly ─────────────────────────────────────────────


async def test_the_stream_helper_stops_at_its_limit(tmp_path: pathlib.Path):
    path = tmp_path / "x.bin"
    path.write_bytes(b"B" * 5000)

    collected = b"".join(
        [chunk async for chunk in _stream_file(str(path), asyncio.get_running_loop(), 1234)]
    )

    assert len(collected) == 1234


async def test_the_stream_helper_without_a_limit_reads_to_eof(tmp_path: pathlib.Path):
    """No limit is still the whole file, for any caller that wants that."""
    path = tmp_path / "x.bin"
    path.write_bytes(b"B" * 5000)

    collected = b"".join(
        [chunk async for chunk in _stream_file(str(path), asyncio.get_running_loop())]
    )

    assert len(collected) == 5000


async def test_the_stream_helper_yields_a_short_file_whole(tmp_path: pathlib.Path):
    """A limit larger than the file is not padding; it just ends."""
    path = tmp_path / "x.bin"
    path.write_bytes(b"B" * 10)

    collected = b"".join(
        [chunk async for chunk in _stream_file(str(path), asyncio.get_running_loop(), 9999)]
    )

    assert collected == b"B" * 10


async def test_a_zero_limit_yields_nothing(tmp_path: pathlib.Path):
    """The degenerate bound, so it cannot become "no limit" by accident."""
    path = tmp_path / "x.bin"
    path.write_bytes(b"B" * 100)

    collected = [chunk async for chunk in _stream_file(str(path), asyncio.get_running_loop(), 0)]

    assert collected == []
