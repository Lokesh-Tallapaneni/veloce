"""Static file serving with caching, ETag, and Last-Modified support.

All file I/O runs in the executor so the event loop is never blocked.
Conditional GET responses follow RFC 9110 §13.1.

Spec anchors:
- RFC 9110 §8.8.2 — Last-Modified header
- RFC 9110 §8.8.3 — ETag header
- RFC 9110 §13.1.3 — If-Modified-Since
- RFC 9110 §13.1.4 — If-None-Match
"""

from __future__ import annotations

import asyncio
import html as _html
import mimetypes
import os
import stat
from collections import OrderedDict
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from veloce._internal import _file_etag
from veloce.http.dates import http_date, parse_date
from veloce.http.request import Request
from veloce.http.response import Response, StreamingResponse
from veloce.safe import safe_join


@lru_cache(maxsize=512)
def _guess_content_type(path: str) -> str:
    """Cache `mimetypes.guess_type` by full path.

    `guess_type` walks the registered MIME table on every call and a
    static-file server hits the same extensions over and over. Caching
    the result keeps the lookup off the hot path. Bounded so a hostile
    client probing arbitrary paths can't grow the cache without bound.
    """
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


class StaticFiles:
    """Serve static files from a directory — all file I/O runs in executor."""

    # Per-instance bound on the ETag cache. Capping it keeps memory
    # bounded for long-running workers serving large static trees —
    # without a cap, every served path lives in the dict forever.
    ETAG_CACHE_MAX = 1024
    # Files at or above this size are streamed in chunks rather than
    # buffered into one bytes object. 1 MiB strikes the balance: small
    # files keep the single-message ASGI fast path, larger files don't
    # hold their full content in worker RSS for the duration of the
    # transfer. Tune by subclassing or assigning on the instance.
    STREAM_THRESHOLD = 1 * 1024 * 1024
    # Chunk size for the streaming path. 64 KiB matches asyncio's
    # default transport write-buffer high-water mark, so chunks ride
    # the wire without rebuffering.
    STREAM_CHUNK_SIZE = 64 * 1024

    def __init__(
        self,
        directory: str,
        prefix: str = "/static",
        html: bool = False,
        directory_index: bool = False,
    ) -> None:
        self.directory = os.path.abspath(directory)
        # The served root's real (symlink-resolved) path. Constant for the
        # life of the handler, so resolve it once here rather than calling
        # realpath on it per request in the containment check.
        self._real_root = os.path.realpath(self.directory)
        self.prefix = prefix.rstrip("/")
        self.html = html
        # Generate an HTML directory listing for paths that resolve to
        # a directory (no `index.html` present). Off by default —
        # directory listings are an information-disclosure risk and
        # most production deployments don't want them.
        self.directory_index = directory_index
        # Bounded LRU: insertion order doubles as recency; the oldest
        # entry is dropped when the cap is hit. Capacity is per-instance
        # so a deployment with many static handlers stays bounded.
        self._etag_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def _is_under_root(self, real_path: str) -> bool:
        """True when `real_path` (already realpath-resolved) is inside the
        served root. Uses `commonpath` so prefix-collisions (e.g.
        `/srv/static_evil/...` against root `/srv/static`) are correctly
        rejected. `ValueError` on Windows drive mismatches counts as out."""
        try:
            return os.path.commonpath([real_path, self._real_root]) == self._real_root
        except ValueError:
            return False

    def _remember_etag(self, key: str, etag: str, mtime: float) -> None:
        """Record an ETag in the LRU, evicting the oldest if at the cap."""
        self._etag_cache[key] = (etag, mtime)
        self._etag_cache.move_to_end(key)
        while len(self._etag_cache) > self.ETAG_CACHE_MAX:
            self._etag_cache.popitem(last=False)

    async def handle(self, request: Request) -> Response | None:
        """Handle a static file request — file I/O offloaded to thread pool."""
        path = request.path
        if not path.startswith(self.prefix):
            return None

        relative = path[len(self.prefix) :].lstrip("/")
        if not relative and self.html:
            relative = "index.html"

        # Security: traversal-safe via safe_join (rejects `..`, absolute
        # components, and NUL bytes — returns None on any escape attempt).
        # Pure string arithmetic; actual filesystem I/O is offloaded below.
        file_path = safe_join(self.directory, relative)  # noqa: ASYNC240
        if file_path is None:
            return Response(status_code=403, body=b"Forbidden")

        loop = asyncio.get_running_loop()

        # Single stat call replaces isfile + later stat. `stat` raises
        # FileNotFoundError on a missing entry, and `S_ISREG`/`S_ISDIR`
        # on the result tells us file-vs-dir without another syscall.
        # `PermissionError` returns a tagged sentinel so we can surface
        # 403 — matching the `safe_join` traversal guard above — rather
        # than letting it bubble to a 500.
        def _try_stat(p: str) -> tuple[os.stat_result | None, bool]:
            """Return (stat_result, permission_denied)."""
            try:
                return (os.stat(p), False)
            except (FileNotFoundError, NotADirectoryError):
                return (None, False)
            except PermissionError:
                return (None, True)

        stat_result, denied = await loop.run_in_executor(None, _try_stat, file_path)
        if denied:
            return Response(status_code=403, body=b"Forbidden")
        is_dir = stat_result is not None and stat.S_ISDIR(stat_result.st_mode)
        is_file = stat_result is not None and stat.S_ISREG(stat_result.st_mode)

        if not is_file:
            # Try the .html-suffixed variant when `html=True` is set
            # (handles `/about` → `/about.html` mappings).
            if self.html and not relative.endswith(".html"):
                file_path_html = file_path + ".html"
                stat_html, denied_html = await loop.run_in_executor(None, _try_stat, file_path_html)
                if denied_html:
                    return Response(status_code=403, body=b"Forbidden")
                if stat_html is not None and stat.S_ISREG(stat_html.st_mode):
                    file_path = file_path_html
                    stat_result = stat_html
                    is_file = True
            if not is_file and self.directory_index and is_dir:
                # Symlink containment: same `commonpath` check as the file
                # path below — single rule for "real path stays under the
                # served root after symlink resolution" prevents a planted
                # symlink in the index path from escaping.
                real = await loop.run_in_executor(None, os.path.realpath, file_path)
                if not self._is_under_root(real):
                    return Response(status_code=403, body=b"Forbidden")
                return await self._render_directory_index(file_path, request.path, loop)
            if not is_file:
                return None

        # Symlink safety: `safe_join` blocks `..` traversal but does not
        # resolve symlinks. Dereference the real path and confirm it is
        # still inside the served root — a symlink planted in the served
        # directory must not expose files elsewhere on the filesystem.
        real_path = await loop.run_in_executor(None, os.path.realpath, file_path)
        if not self._is_under_root(real_path):
            return Response(status_code=403, body=b"Forbidden")

        # stat_result was populated by the existence check above; reuse it.
        assert stat_result is not None  # narrowed by the `not is_file` returns
        mtime = stat_result.st_mtime
        size = stat_result.st_size
        cache_key = file_path

        if cache_key in self._etag_cache:
            cached_etag, cached_mtime = self._etag_cache[cache_key]
            if cached_mtime == mtime:
                etag = cached_etag
                # Record the hit as recent usage so the LRU keeps it.
                self._etag_cache.move_to_end(cache_key)
            else:
                etag = self._compute_etag(file_path, size, mtime)
                self._remember_etag(cache_key, etag, mtime)
        else:
            etag = self._compute_etag(file_path, size, mtime)
            self._remember_etag(cache_key, etag, mtime)

        last_modified = http_date(mtime)

        # Conditional GET. Per RFC 9110 §13.2 precedence: If-None-Match
        # supersedes If-Modified-Since when both are present.
        if_none_match = request.headers.get("if-none-match", "")
        if if_none_match:
            if if_none_match.strip() == "*":
                return Response(
                    status_code=304,
                    body=b"",
                    headers={"ETag": etag, "Last-Modified": last_modified},
                )
            client_etags = [t.strip().strip('"') for t in if_none_match.split(",")]
            if etag.strip('"') in client_etags:
                return Response(
                    status_code=304,
                    body=b"",
                    headers={"ETag": etag, "Last-Modified": last_modified},
                )
        else:
            ims_header = request.headers.get("if-modified-since", "")
            ims_dt = parse_date(ims_header)
            ims_ts = ims_dt.timestamp() if ims_dt is not None else None
            # Floor mtime to whole seconds because HTTP-dates have second
            # resolution; otherwise `mtime=1.5` would always appear
            # "newer" than `IMS=1`.
            if ims_ts is not None and int(mtime) <= int(ims_ts):
                return Response(
                    status_code=304,
                    body=b"",
                    headers={"ETag": etag, "Last-Modified": last_modified},
                )

        content_type = _guess_content_type(file_path)

        # Range request — RFC 9110 §14.2. Single-range only; multi-range
        # would require multipart/byteranges which we don't ship yet.
        range_spec = request.range
        if range_spec is not None and range_spec.unit == "bytes" and len(range_spec.ranges) == 1:
            start, end = range_spec.ranges[0]
            if start is None and end is None:
                resolved = None
            elif start is None:
                # Suffix range: last `end` bytes. `bytes=-500` over a 200-byte
                # file should return the whole file, per RFC 9110 §14.1.2.
                suffix = min(end or 0, size)
                resolved = (size - suffix, size - 1) if suffix > 0 else None
            else:
                resolved = (start, end if end is not None and end < size else size - 1)

            if resolved is None or resolved[0] >= size or resolved[0] > resolved[1]:
                return Response(
                    status_code=416,
                    body=b"",
                    headers={
                        "Content-Range": f"bytes */{size}",
                        "ETag": etag,
                        "Last-Modified": last_modified,
                        "Accept-Ranges": "bytes",
                    },
                )

            r_start, r_end = resolved
            length = r_end - r_start + 1

            def _read_range() -> bytes:
                with open(file_path, "rb") as f:
                    f.seek(r_start)
                    return f.read(length)

            body = await loop.run_in_executor(None, _read_range)
            return Response(
                status_code=206,
                body=body,
                content_type=content_type,
                headers={
                    "Content-Range": f"bytes {r_start}-{r_end}/{size}",
                    "ETag": etag,
                    "Last-Modified": last_modified,
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "public, max-age=3600",
                },
            )

        common_headers = {
            "ETag": etag,
            "Last-Modified": last_modified,
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
        }

        # Files at or above `STREAM_THRESHOLD` use chunked streaming so
        # the whole file never sits in memory at once — a single large
        # download (or many concurrent ones) no longer balloons the
        # worker's RSS by the file size. Smaller files stay buffered
        # because (a) the response is one ASGI message instead of N,
        # and (b) the per-chunk syscall overhead dominates at small
        # sizes. Range responses always buffer their slice — a range is
        # already bounded by the client.
        if size >= self.STREAM_THRESHOLD:

            # Don't emit `Content-Length` alongside chunked transfer —
            # RFC 9112 §6.1 forbids carrying both, and a strict proxy
            # may drop or 502 the response. Clients that need a
            # progress hint can issue a HEAD or read `ETag`.
            return StreamingResponse(
                content=self._iter_file(file_path, loop),
                status_code=200,
                content_type=content_type,
                headers=dict(common_headers),
            )

        def _read() -> bytes:
            with open(file_path, "rb") as f:
                return f.read()

        body = await loop.run_in_executor(None, _read)

        return Response(
            status_code=200,
            body=body,
            content_type=content_type,
            headers=common_headers,
        )

    async def _iter_file(self, path: str, loop: Any) -> AsyncIterator[bytes]:
        """Yield the file in `STREAM_CHUNK_SIZE`-byte chunks via the executor.

        The file handle is opened on the executor (blocking syscall) and
        closed in a finally so a client disconnect mid-stream doesn't
        leak a descriptor. Each `read` runs on the executor too — the
        event loop stays responsive while a slow disk delivers bytes.
        """

        def _open() -> Any:
            return open(path, "rb")  # noqa: SIM115 — closed in finally

        chunk_size = self.STREAM_CHUNK_SIZE
        fh = await loop.run_in_executor(None, _open)
        try:
            while True:
                chunk = await loop.run_in_executor(None, fh.read, chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            await loop.run_in_executor(None, fh.close)

    def _compute_etag(self, path: str, size: int, mtime: float) -> str:
        """Compute ETag — delegates to the shared `_file_etag` helper so the
        StaticFiles handler and `FileResponse` validate against the same
        `If-None-Match` value for the same file."""

        return _file_etag(path, size, mtime)

    async def _render_directory_index(self, dir_path: str, url_path: str, loop: Any) -> Response:
        """Render an HTML index of `dir_path`'s entries.

        Entries are HTML-escaped via `_html.escape` so a filename
        containing `<script>` can't poison the page. Subdirectories
        get a trailing slash. Hidden files (`.foo`) are omitted —
        matches nginx `autoindex on;` default.
        """
        def _list_dir() -> list[tuple[str, bool]]:
            """Return `(name, is_dir)` tuples for the directory.

            `os.scandir` answers `is_dir()` from cached stat data on the
            same syscall that produced the entry, so we don't need a
            second `os.path.isdir` per item.
            """
            try:
                with os.scandir(dir_path) as it:
                    return sorted(
                        (
                            (e.name, e.is_dir(follow_symlinks=False))
                            for e in it
                            if not e.name.startswith(".")
                        ),
                        key=lambda t: t[0],
                    )
            except OSError:
                return []

        entries = await loop.run_in_executor(None, _list_dir)
        base = url_path if url_path.endswith("/") else url_path + "/"

        rows: list[str] = []
        # Parent-directory link unless we're at the prefix root.
        if base.rstrip("/") != self.prefix:
            rows.append('<li><a href="../">../</a></li>')

        for name, is_dir in entries:
            display = _html.escape(name) + ("/" if is_dir else "")
            rows.append(
                f'<li><a href="{_html.escape(name)}{("/" if is_dir else "")}">{display}</a></li>'
            )

        title = f"Index of {_html.escape(base)}"
        body = (
            "<!doctype html><html><head>"
            f"<title>{title}</title>"
            '<meta charset="utf-8">'
            "</head><body>"
            f"<h1>{title}</h1><ul>" + "".join(rows) + "</ul></body></html>"
        )
        return Response(
            status_code=200,
            body=body.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )
