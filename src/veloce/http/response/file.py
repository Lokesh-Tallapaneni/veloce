"""File responses — range negotiation, conditional headers and chunked file streaming."""

from __future__ import annotations

import asyncio
import os
import stat
import warnings
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, BinaryIO

from veloce._constants import (
    HEADER_ACCEPT_RANGES,
    HEADER_CONTENT_DISPOSITION,
    HEADER_CONTENT_LENGTH,
    HEADER_CONTENT_TYPE,
    HEADER_ETAG,
    HEADER_LAST_MODIFIED,
    HEADER_TRANSFER_ENCODING,
    HEADER_VALUE_ATTACHMENT,
    HEADER_VALUE_BYTES,
    HEADER_VALUE_CHUNKED,
)
from veloce._internal import (
    _etag_matches_strong,
    _file_etag,
    _write_chunked,
    guess_content_type,
)
from veloce._protocol_constants import (
    HTTP_METHOD_GET,
)
from veloce._warnings import VeloceDeprecationWarning
from veloce.http.dates import http_date
from veloce.status import (
    HTTP_200_OK,
    HTTP_206_PARTIAL_CONTENT,
    HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
)

if TYPE_CHECKING:  # pragma: no cover
    # Type-only: a runtime import closes a response->request->exceptions cycle.
    from veloce.http.request import Request

# ── Case-insensitive response-header access ──────────────────────────
# `Response.headers` is case-SENSITIVE but HTTP field names are not (RFC 9110
# Sec. 5.1), so these fold the lookup without allocating a dict per call.
from veloce.http.response._headers import _format_content_disposition, header_get
from veloce.http.response.response import Response


async def _stream_file(path: str, loop: Any, limit: int | None = None, start: int = 0) -> Any:
    """Yield a file's bytes in chunks, each read in the executor, up to `limit`.

    The open and every read are offloaded, so no disk I/O runs on the event
    loop, and only one chunk is resident at a time. `start` seeks before the
    first read, so a `206` serves its slice without reading what precedes it.

    `limit` is the `Content-Length` the response already declared, and bounds
    what a file appended to since that stat can yield: on the native transport
    the surplus lands on the keep-alive connection and is read as the start of
    the next response. `None` reads to EOF, for a caller with no declared length
    to honour.
    """
    handle = await loop.run_in_executor(None, _open_file_binary, path, start)
    remaining = limit
    try:
        while remaining is None or remaining > 0:
            chunk = await loop.run_in_executor(None, _read_file_chunk, handle)
            if not chunk:
                return
            if remaining is not None:
                if len(chunk) >= remaining:
                    yield chunk[:remaining]
                    return
                remaining -= len(chunk)
            yield chunk
    finally:
        await loop.run_in_executor(None, handle.close)


def _open_file_binary(path: str, start: int = 0) -> BinaryIO:
    """Open `path` for binary reading, positioned at `start` - run in an executor."""
    handle = open(path, "rb")  # noqa: SIM115 - closed by `_stream_file`
    if start:
        # Seeking here keeps the open and the seek in one executor hop.
        handle.seek(start)
    return handle


FILE_STREAM_CHUNK: int = 64 * 1024
"""Bytes per chunk read off disk: amortises the executor hop and bounds the download."""


def _read_file_chunk(handle: BinaryIO) -> bytes:
    """Read one chunk from an open file - run in an executor."""
    return handle.read(FILE_STREAM_CHUNK)


# At or below this size `FileResponse.from_path` reads inline; above it, a
# thread-pool executor. The hop costs ~100 us measured, dominating the
# few-microsecond read of a small asset, and 64 KiB stays sub-millisecond.
_INLINE_READ_MAX = 64 * 1024


def _stat_regular_file(path: str) -> os.stat_result:
    """Stat `path`, raising `FileNotFoundError` for a missing or non-regular file.

    One stat covers the regular-file guard and the mtime/size used for
    `Last-Modified` + `ETag`; an `os.path.isfile` pre-check would stat twice.
    """
    try:
        st = os.stat(path)
    except OSError as err:
        raise FileNotFoundError(f"File not found: {path}") from err
    if not stat.S_ISREG(st.st_mode):
        raise FileNotFoundError(f"File not found: {path}")
    return st


def _build_file_headers(
    path: str,
    st: os.stat_result,
    filename: str | None,
    content_type: str | None,
    content_disposition_type: str,
    headers: dict[str, str] | None,
    *,
    advertise_ranges: bool,
) -> tuple[str, dict[str, str]]:
    """Resolve the content type and build the file-serving response headers.

    Shared by the sync `FileResponse(...)` and the async `from_path(...)` so the
    `Content-Disposition` / `Last-Modified` / `ETag` shape stays identical on
    both paths.
    """
    if content_type is None:
        content_type = guess_content_type(path)

    # Copied: a shared `HEADERS = {...}` would answer `304` for an unseen body.
    hdrs = dict(headers) if headers else {}
    if filename:
        hdrs[HEADER_CONTENT_DISPOSITION] = _format_content_disposition(
            content_disposition_type, filename
        )
    elif content_disposition_type != HEADER_VALUE_ATTACHMENT:
        # Unset without a filename, so `FileResponse(path)` forces no download.
        hdrs[HEADER_CONTENT_DISPOSITION] = content_disposition_type

    if HEADER_LAST_MODIFIED not in hdrs and "last-modified" not in hdrs:
        hdrs[HEADER_LAST_MODIFIED] = http_date(st.st_mtime)
    if HEADER_ETAG not in hdrs and "etag" not in hdrs:
        hdrs[HEADER_ETAG] = _file_etag(path, st.st_size, st.st_mtime)
    if advertise_ranges and HEADER_ACCEPT_RANGES not in hdrs and "accept-ranges" not in hdrs:
        # A client resumes only if the first response advertised it (RFC 9110 14.3).
        hdrs[HEADER_ACCEPT_RANGES] = HEADER_VALUE_BYTES
    return content_type, hdrs


def _resolve_byte_range(span: tuple[int | None, int | None], size: int) -> tuple[int, int] | None:
    """Turn one parsed `bytes=` range into absolute offsets, or `None` if unsatisfiable.

    `None` covers a suffix range of zero bytes and anything out of bounds; the
    caller answers those with a `416`. A zero-length representation has no
    satisfiable range at all (RFC 9110 Sec. 14.1.2).
    """
    start, end = span
    if start is None:
        # Suffix range: `bytes=-500` over a 200-byte file is the whole file.
        suffix = min(end or 0, size)
        return (size - suffix, size - 1) if suffix > 0 else None
    if start >= size:
        return None
    if end is None or end >= size:
        end = size - 1
    return (start, end) if start <= end else None


#: The lowercase wire key for `Range`, encoded once for `_peek_header_key`.
_RAW_RANGE = b"range"


def _negotiate_file_range(
    request: Request | None, hdrs: dict[str, str], size: int, mtime: float
) -> tuple[bool, tuple[int, int] | None]:
    """Resolve a request's `Range` against a file of `size` bytes.

    Returns `(satisfiable, span)`: `(True, None)` to serve the whole file,
    `(True, (start, end))` to serve that inclusive slice as a `206`, and
    `(False, None)` to answer `416`.

    `request=None` means "do not negotiate": a `Range` applies only to the
    representation the client asked for, so negotiation is opt-in and the
    helpers that serve the *requested* file - `send_file`,
    `send_from_directory`, `StaticFiles` - are what opt in.

    `GET` is the only method range handling is defined for, and a `Range` on any
    other MUST be ignored (RFC 9110 Sec. 14.2) - including `HEAD`, which
    `Accept-Ranges` already answers for a client probing support (Sec. 14.3).

    The `Range` lookup scans the raw header block rather than `request.headers`,
    because materialising that mapping costs ~2.7 us a handler serving a file
    need not have spent (see `Request._peek_header_key`).
    """
    if request is None or request.method != HTTP_METHOD_GET:
        return True, None
    if request._peek_header_key(_RAW_RANGE) is None:
        return True, None

    if header_get(hdrs, HEADER_ACCEPT_RANGES) != HEADER_VALUE_BYTES:
        return True, None

    spec = request.range
    if spec is None or spec.unit != HEADER_VALUE_BYTES or len(spec.ranges) != 1:
        # RFC 9110 Sec. 14.2 lets a recipient ignore a `Range` it will not act on.
        return True, None

    # If-Range (RFC 9110 Sec. 13.1.5): a stale validator drops the range.
    if_etag, if_date = request.if_range
    if if_etag:
        # Strong comparison: a resume needs byte identity, not equivalence.
        if not _etag_matches_strong(header_get(hdrs, HEADER_ETAG) or "", if_etag):
            return True, None
    elif if_date is not None and int(mtime) != int(if_date):
        # An exact date match, not the "not later than" `If-Unmodified-Since` test.
        return True, None

    span = _resolve_byte_range(spec.ranges[0], size)
    return (True, span) if span is not None else (False, None)


class FileResponse(Response):
    """Serve a file from disk - small files inline, large files via executor.

    Advertises `Accept-Ranges: bytes` and honours a single-range `Range:` header
    on the `GET` being served, answering `206` with a `Content-Range` for a
    satisfiable range and `416` for one past the end of the file. A multi-range
    request is served in full, as is one whose `If-Range` validator is stale and
    one on any other method - `GET` is the only method range handling is defined
    for (RFC 9110 Sec. 14.2).

    `If-Range` is compared strongly, as Sec. 13.1.5 requires, so the weak `ETag`
    these responses carry by default cannot authorize a resume: a client echoing
    it back is served the full representation. Resume against `Last-Modified`,
    or pass a strong `ETag` of your own in `headers`.
    """

    __slots__ = ()

    def __init__(
        self,
        path: str,
        filename: str | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
        content_disposition_type: str = HEADER_VALUE_ATTACHMENT,
        request: Request | None = None,
    ) -> None:
        st = _stat_regular_file(path)
        content_type, hdrs = _build_file_headers(
            path,
            st,
            filename,
            content_type,
            content_disposition_type,
            headers,
            advertise_ranges=request is not None,
        )

        # On a running loop a 50 MB read stalls everything; a warning, not a raise.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            warnings.warn(
                "FileResponse(path) does a blocking read on the running "
                "event loop. Use `await FileResponse.from_path(path, ...)` "
                "from async handlers, or wrap the sync call in "
                "`asyncio.to_thread(...)`. This will raise in a future "
                "release.",
                VeloceDeprecationWarning,
                stacklevel=2,
            )
        satisfiable, span = _negotiate_file_range(request, hdrs, st.st_size, st.st_mtime)
        if not satisfiable:
            status = HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE
            body = b""
            served: tuple[int | None, int | None, int] | None = (None, None, st.st_size)
        elif span is None:
            with open(path, "rb") as f:
                body = f.read()
            status = HTTP_200_OK
            served = None
        else:
            start, end = span
            with open(path, "rb") as f:
                f.seek(start)
                body = f.read(end - start + 1)
            status = HTTP_206_PARTIAL_CONTENT
            served = (start, end, st.st_size)

        super().__init__(
            status_code=status,
            body=body,
            content_type=content_type,
            headers=hdrs,
        )
        if served is not None:
            # Onto its own headers: this describes one slice of one request.
            self.set_content_range(*served)

    async def stream_to(
        self,
        transport: Any,
        drain: Callable[[], Awaitable[None]] | None = None,
        *,
        keep_alive: bool = True,
    ) -> None:
        """Write the head, then the file's chunks, on the raw transport.

        A file's length is known, so the body is normally length-delimited
        rather than chunk-framed - which is why this does not go through
        `StreamingResponse.stream_to`. A response middleware may legitimately
        remove the `Content-Length` (`CompressionMiddleware` does for every
        streamed body), and with no declared length there is nothing to delimit
        the body, so it is chunk-framed instead. `drain` throttles a producer
        outrunning a slow client in both cases.

        Args:
            transport: The raw transport to write to.
            drain: Awaited after each write to apply flow control; `None` skips it.
            keep_alive: Whether to advertise a persistent connection.

        """
        if header_get(self.headers, HEADER_CONTENT_LENGTH) is None:
            # `encode()` would emit `Content-Length: 0` beside the chunked framing.
            transport.write(
                self._encode_streaming_head(
                    {
                        HEADER_CONTENT_TYPE: self.content_type,
                        HEADER_TRANSFER_ENCODING: HEADER_VALUE_CHUNKED,
                    },
                    keep_alive,
                )
            )
            await _write_chunked(transport, self._stream, drain)
            return

        transport.write(self.encode(keep_alive=keep_alive))
        async for chunk in self._stream:
            transport.write(chunk)
            if drain is not None:
                await drain()

    @classmethod
    async def from_path(
        cls,
        path: str,
        filename: str | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
        content_disposition_type: str = HEADER_VALUE_ATTACHMENT,
        request: Request | None = None,
    ) -> FileResponse:
        """Async factory - reads small files inline, large files in the executor.

        A slice at or below `_INLINE_READ_MAX` is read inline, skipping the
        thread-pool hop that would dominate serving a small static asset; a
        larger one is read in the executor so a big read never stalls the loop.
        Honours `Range:` on the same terms as the sync constructor.

        Args:
            path: File to serve.
            filename: Download name; adds a `Content-Disposition` header when set.
            content_type: Overrides the type guessed from the file name.
            headers: Extra response headers.
            content_disposition_type: `attachment` or `inline`.
            request: The request, used to honour `Range:` and conditional headers.

        Returns:
            The response; a 416 when the requested range cannot be satisfied.

        Raises:
            FileNotFoundError: If `path` is missing or not a regular file.

        """  # noqa: DOC502 - the exception is raised or caught by a callee
        loop = asyncio.get_running_loop()

        st = _stat_regular_file(path)

        content_type, hdrs = _build_file_headers(
            path,
            st,
            filename,
            content_type,
            content_disposition_type,
            headers,
            advertise_ranges=request is not None,
        )

        satisfiable, span = _negotiate_file_range(request, hdrs, st.st_size, st.st_mtime)
        if not satisfiable:
            resp = Response.__new__(cls)
            Response.__init__(
                resp,
                status_code=HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                body=b"",
                content_type=content_type,
                headers=hdrs,
            )
            resp.set_content_range(None, None, st.st_size)
            return resp

        if span is None:
            start, length, status = 0, st.st_size, HTTP_200_OK
            served: tuple[int, int, int] | None = None
        else:
            start, end = span
            length = end - start + 1
            status = HTTP_206_PARTIAL_CONTENT
            served = (start, end, st.st_size)

        # Applied to the slice served, not the file: a bounded range is one read.
        if length <= _INLINE_READ_MAX:
            # ASYNC230: a deliberate <= 64 KiB read, avoiding the ~100 us hop.
            with open(path, "rb") as f:  # noqa: ASYNC230 - runs in a worker thread, not on the event loop
                if span is None:
                    body = f.read()
                else:
                    f.seek(start)
                    body = f.read(length)
            resp = Response.__new__(cls)
            Response.__init__(
                resp, status_code=status, body=body, content_type=content_type, headers=hdrs
            )
            if served is not None:
                resp.set_content_range(*served)
            return resp

        # Streamed off disk, because reading whole made RSS scale with (file size x
        # concurrent requests) - four concurrent 32 MiB downloads measured
        # 134 MB RSS. The stat gives the length, so it stays length-delimited.
        resp = Response.__new__(cls)
        Response.__init__(
            resp, status_code=status, body=b"", content_type=content_type, headers=hdrs
        )
        if served is not None:
            resp.set_content_range(*served)
        resp.headers[HEADER_CONTENT_LENGTH] = str(length)
        # The length the header declares, so a growing file cannot disagree.
        resp._stream = _stream_file(path, loop, length, start)
        return resp
