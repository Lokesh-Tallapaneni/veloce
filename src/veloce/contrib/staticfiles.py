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
import hashlib
import mimetypes
import os
from email.utils import formatdate, parsedate_to_datetime
from typing import Any

from veloce.http.request import Request
from veloce.http.response import Response


def _format_http_date(timestamp: float) -> str:
    """Format a Unix timestamp as an IMF-fixdate per RFC 9110 §5.6.7."""
    return formatdate(timestamp, usegmt=True)


def _parse_http_date(value: str) -> float | None:
    """Parse an HTTP-date into a Unix timestamp. Return None on failure.

    Accepts the three syntaxes RFC 9110 §5.6.7 lists: IMF-fixdate,
    obsolete RFC 850, and ANSI C `asctime()`. `email.utils.parsedate_to_datetime`
    handles all three.
    """
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt.timestamp()


class StaticFiles:
    """Serve static files from a directory — all file I/O runs in executor."""

    def __init__(
        self,
        directory: str,
        prefix: str = "/static",
        html: bool = False,
        directory_index: bool = False,
    ) -> None:
        self.directory = os.path.abspath(directory)
        self.prefix = prefix.rstrip("/")
        self.html = html
        # Generate an HTML directory listing for paths that resolve to
        # a directory (no `index.html` present). Off by default —
        # directory listings are an information-disclosure risk and
        # most production deployments don't want them.
        self.directory_index = directory_index
        self._etag_cache: dict[str, tuple[str, float]] = {}

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
        from veloce.safe import safe_join

        file_path = safe_join(self.directory, relative)  # noqa: ASYNC240
        if file_path is None:
            return Response(status_code=403, body=b"Forbidden")

        loop = asyncio.get_running_loop()

        # Offload stat/isfile checks to executor.
        exists = await loop.run_in_executor(None, os.path.isfile, file_path)
        if not exists:
            # Try the .html-suffixed variant when `html=True` is set
            # (handles `/about` → `/about.html` mappings).
            if self.html and not relative.endswith(".html"):
                file_path_html = file_path + ".html"
                exists_html = await loop.run_in_executor(None, os.path.isfile, file_path_html)
                if exists_html:
                    file_path = file_path_html
                    exists = True
            if not exists and self.directory_index:
                # Last-chance fallback: render an HTML directory listing
                # when the path resolves to a real dir.
                is_dir = await loop.run_in_executor(None, os.path.isdir, file_path)
                if is_dir:
                    return await self._render_directory_index(file_path, request.path, loop)
            if not exists:
                return None

        stat_result = await loop.run_in_executor(None, os.stat, file_path)
        mtime = stat_result.st_mtime
        cache_key = file_path

        if cache_key in self._etag_cache:
            cached_etag, cached_mtime = self._etag_cache[cache_key]
            if cached_mtime == mtime:
                etag = cached_etag
            else:
                etag = self._compute_etag(file_path, mtime)
                self._etag_cache[cache_key] = (etag, mtime)
        else:
            etag = self._compute_etag(file_path, mtime)
            self._etag_cache[cache_key] = (etag, mtime)

        last_modified = _format_http_date(mtime)

        # Conditional GET. Per RFC 9110 §13.2 precedence: If-None-Match
        # supersedes If-Modified-Since when both are present.
        if_none_match = request.headers.get("if-none-match", "")
        if if_none_match:
            if if_none_match == etag or if_none_match == "*":
                return Response(
                    status_code=304,
                    body=b"",
                    headers={"ETag": etag, "Last-Modified": last_modified},
                )
        else:
            ims_header = request.headers.get("if-modified-since", "")
            ims_ts = _parse_http_date(ims_header)
            # Floor mtime to whole seconds because HTTP-dates have second
            # resolution; otherwise `mtime=1.5` would always appear
            # "newer" than `IMS=1`.
            if ims_ts is not None and int(mtime) <= int(ims_ts):
                return Response(
                    status_code=304,
                    body=b"",
                    headers={"ETag": etag, "Last-Modified": last_modified},
                )

        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        size = stat_result.st_size

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

        def _read() -> bytes:
            with open(file_path, "rb") as f:
                return f.read()

        body = await loop.run_in_executor(None, _read)

        return Response(
            status_code=200,
            body=body,
            content_type=content_type,
            headers={
                "ETag": etag,
                "Last-Modified": last_modified,
                "Accept-Ranges": "bytes",
                "Cache-Control": "public, max-age=3600",
            },
        )

    def _compute_etag(self, path: str, mtime: float) -> str:
        """Compute ETag from file path and modification time."""
        key = f"{path}:{mtime}".encode()
        return f'"{hashlib.md5(key).hexdigest()}"'

    async def _render_directory_index(self, dir_path: str, url_path: str, loop: Any) -> Response:
        """Render an HTML index of `dir_path`'s entries.

        Entries are HTML-escaped via `html.escape` so a filename
        containing `<script>` can't poison the page. Subdirectories
        get a trailing slash. Hidden files (`.foo`) are omitted —
        matches nginx `autoindex on;` default.
        """
        import html

        def _list_dir() -> list[str]:
            try:
                names = sorted(os.listdir(dir_path))
            except OSError:
                return []
            return [n for n in names if not n.startswith(".")]

        names = await loop.run_in_executor(None, _list_dir)
        base = url_path if url_path.endswith("/") else url_path + "/"

        rows: list[str] = []
        # Parent-directory link unless we're at the prefix root.
        if base.rstrip("/") != self.prefix:
            rows.append('<li><a href="../">../</a></li>')

        for name in names:
            entry_path = os.path.join(dir_path, name)
            is_dir = await loop.run_in_executor(None, os.path.isdir, entry_path)
            display = html.escape(name) + ("/" if is_dir else "")
            rows.append(
                f'<li><a href="{html.escape(name)}{("/" if is_dir else "")}">{display}</a></li>'
            )

        title = f"Index of {html.escape(base)}"
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
