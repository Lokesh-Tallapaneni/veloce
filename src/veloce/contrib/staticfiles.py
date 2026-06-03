"""Static file serving with caching, ETag, and Last-Modified support.

All file I/O runs in the executor so the event loop is never blocked.
Conditional GET responses follow RFC 9110 Sec. 13.1.

Spec anchors:
- RFC 9110 Sec. 8.8.2 - Last-Modified header
- RFC 9110 Sec. 8.8.3 - ETag header
- RFC 9110 Sec. 13.1.1 - If-Match
- RFC 9110 Sec. 13.1.3 - If-Modified-Since
- RFC 9110 Sec. 13.1.4 - If-None-Match
- RFC 9110 Sec. 13.1.4 - If-Unmodified-Since
- RFC 9110 Sec. 13.2.2 - precondition precedence
"""

from __future__ import annotations

import asyncio
import html
import mimetypes
import os
import stat
import warnings
from collections import OrderedDict
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from veloce._constants import (
    HEADER_ACCEPT_ENCODING,
    HEADER_ACCEPT_RANGES,
    HEADER_CACHE_CONTROL,
    HEADER_CONTENT_ENCODING,
    HEADER_CONTENT_RANGE,
    HEADER_ETAG,
    HEADER_IF_MODIFIED_SINCE,
    HEADER_IF_NONE_MATCH,
    HEADER_LAST_MODIFIED,
    HEADER_VALUE_BYTES,
    HEADER_VARY,
    MIME_OCTET_STREAM,
    MIME_TEXT_HTML_UTF8,
)
from veloce._internal import _etag_matches_strong, _etag_matches_weak, _file_etag
from veloce.http.dates import http_date, parse_date
from veloce.http.request import Request
from veloce.http.response import Response, StreamingResponse
from veloce.safe import safe_join
from veloce.status import (
    HTTP_200_OK,
    HTTP_206_PARTIAL_CONTENT,
    HTTP_304_NOT_MODIFIED,
    HTTP_403_FORBIDDEN,
    HTTP_412_PRECONDITION_FAILED,
    HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
)


def _precondition_failed(
    if_match: tuple[str, ...],
    if_unmodified_since: float | None,
    etag: str,
    mtime: float,
) -> bool:
    """RFC 9110 Sec. 13.1.1 / 13.1.4 write-side preconditions for a static GET.

    Returns True when the request must be rejected with 412. `If-Match`
    takes precedence; `If-Unmodified-Since` is evaluated only when `If-Match`
    is absent (Sec. 13.2.2). Veloce emits weak file ETags and `If-Match`
    mandates the strong comparison (Sec. 8.8.3.1), so any concrete `If-Match`
    list fails closed to 412; `*` matches because the representation exists.
    """
    if if_match:
        if if_match == ("*",):
            return False
        # No strong match across the list -> precondition fails (412).
        return all(not _etag_matches_strong(etag, token) for token in if_match)
    if if_unmodified_since is not None:
        # HTTP-dates carry second resolution - floor mtime to compare.
        return int(mtime) > int(if_unmodified_since)
    return False


@lru_cache(maxsize=512)
def _guess_content_type(path: str) -> str:
    """Cache `mimetypes.guess_type` by full path.

    `guess_type` walks the registered MIME table on every call and a
    static-file server hits the same extensions over and over. Caching
    the result keeps the lookup off the hot path. Bounded so a hostile
    client probing arbitrary paths can't grow the cache without bound.
    """
    return mimetypes.guess_type(path)[0] or MIME_OCTET_STREAM


class StaticFiles:
    """Serve static files from a directory - all file I/O runs in executor."""

    # Per-instance bound on the ETag cache. Capping it keeps memory
    # bounded for long-running workers serving large static trees -
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
    # Content-Encoding token -> precompressed sibling suffix. Probed in
    # this order only to break q-value ties; the actual selection is
    # driven by the client's `Accept-Encoding` q-values (see
    # `_select_precompressed`). `br` precedes `gzip` because it is the
    # denser codec and the conventional preference when quality is equal.
    PRECOMPRESSED_VARIANTS = {"br": ".br", "gzip": ".gz"}

    def __init__(
        self,
        directory: str,
        prefix: str = "/static",
        html: bool = False,
        directory_index: bool = False,
        must_exist: bool = True,
        precompressed: bool = False,
    ) -> None:
        self.directory = os.path.abspath(directory)
        # Validate the configured directory once at construction (a setup-time
        # context, not an async request path). A typo otherwise builds a
        # handler that silently 404s every asset, discoverable only by hitting
        # a URL. `must_exist=False` downgrades the failure to a warning for the
        # dev flow that creates the directory after wiring the app.
        if not os.path.isdir(self.directory):
            problem = (
                f"StaticFiles directory {directory!r} (resolved to "
                f"{self.directory!r}) does not exist or is not a directory"
            )
            if must_exist:
                raise ValueError(problem)
            warnings.warn(problem, stacklevel=2)
        elif not os.access(self.directory, (os.R_OK | os.X_OK) if directory_index else os.X_OK):
            # Serving a known file needs SEARCH (X_OK) on the directory; a
            # directory listing additionally needs READ (R_OK). Require exactly
            # what `handle()` will use, so the check neither false-rejects a
            # search-only dir nor passes a (listing) dir that can't serve files.
            need = "readable and searchable" if directory_index else "searchable"
            problem = (
                f"StaticFiles directory {directory!r} (resolved to "
                f"{self.directory!r}) exists but is not {need}"
            )
            if must_exist:
                raise ValueError(problem)
            warnings.warn(problem, stacklevel=2)
        # The served root's real (symlink-resolved) path. Constant for the
        # life of the handler, so resolve it once here rather than calling
        # realpath on it per request in the containment check.
        self._real_root = os.path.realpath(self.directory)
        self.prefix = prefix.rstrip("/")
        self.html = html
        # Generate an HTML directory listing for paths that resolve to
        # a directory (no `index.html` present). Off by default -
        # directory listings are an information-disclosure risk and
        # most production deployments don't want them.
        self.directory_index = directory_index
        # Serve a precompressed sibling (`app.css.br` / `app.css.gz`) when
        # the client advertises a matching `Accept-Encoding`. Off by default:
        # the variants must be generated ahead of time and the feature adds
        # one extra `stat` per request when enabled, so it is opt-in.
        self.precompressed = precompressed
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

    async def _select_precompressed(
        self,
        request: Request,
        file_path: str,
        loop: Any,
        try_stat: Any,
    ) -> tuple[str, str, os.stat_result] | None:
        """Pick a precompressed sibling for `file_path`, or None.

        Returns `(variant_path, encoding, stat_result)` for the highest-quality
        accepted encoding whose sibling exists on disk as a regular file,
        otherwise None. The client's preference is honoured in descending
        q-value order: if the top encoding has no variant on disk we fall
        through to the next accepted one (RFC 9110 Sec. 12.5.3 - a server may
        serve any acceptable representation), so `br;q=1, gzip;q=0.5` with only
        `app.css.gz` present serves gzip rather than the uncompressed asset.
        The q>0 gate is load-bearing: a missing `Accept-Encoding` header
        expresses no preference and must not falsely select a variant.
        Permission errors on the sibling propagate as 403 (via the caller's
        sentinel) to match the read policy on the original file.
        """
        if not self.precompressed:
            return None
        # Score each on-disk-capable variant by the client's q-value, then
        # probe in descending quality. `PRECOMPRESSED_VARIANTS` insertion order
        # (br before gzip) breaks exact q ties toward the denser codec. The
        # negated index keeps the sort stable on that order without reversing.
        accept = request.accept_encodings
        order = list(self.PRECOMPRESSED_VARIANTS)
        # RFC 9110 Sec. 12.5.3: an explicit `q=0` is an explicit rejection that
        # must override a permissive `*` wildcard. `quality()` returns the MAX
        # across an exact and a `*` match, so `br;q=0, *;q=1` would wrongly score
        # br at 1.0. `quality_explicit()` honors an explicit token's q and only
        # falls back to the wildcard for codings not explicitly listed.
        scored = [
            (q, -idx, enc)
            for idx, enc in enumerate(order)
            if (q := accept.quality_explicit(enc)) > 0
        ]
        if not scored:
            return None
        scored.sort(reverse=True)
        for _q, _tie, enc in scored:
            variant_path = file_path + self.PRECOMPRESSED_VARIANTS[enc]
            variant_stat, denied = await loop.run_in_executor(None, try_stat, variant_path)
            if denied:
                # Surface as 403 by returning the denial to the caller via a raise.
                raise PermissionError(variant_path)
            if variant_stat is not None and stat.S_ISREG(variant_stat.st_mode):
                return (variant_path, enc, variant_stat)
        return None

    async def handle(self, request: Request) -> Response | None:
        """Handle a static file request - file I/O offloaded to thread pool."""
        path = request.path
        if not path.startswith(self.prefix):
            return None

        relative = path[len(self.prefix) :].lstrip("/")
        if not relative and self.html:
            relative = "index.html"

        # Security: traversal-safe via safe_join (rejects `..`, absolute
        # components, and NUL bytes - returns None on any escape attempt).
        # Pure string arithmetic; actual filesystem I/O is offloaded below.
        file_path = safe_join(self.directory, relative)  # noqa: ASYNC240
        if file_path is None:
            return Response(status_code=HTTP_403_FORBIDDEN, body=b"Forbidden")

        loop = asyncio.get_running_loop()

        # Single stat call replaces isfile + later stat. `stat` raises
        # FileNotFoundError on a missing entry, and `S_ISREG`/`S_ISDIR`
        # on the result tells us file-vs-dir without another syscall.
        # `PermissionError` returns a tagged sentinel so we can surface
        # 403 - matching the `safe_join` traversal guard above - rather
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
            return Response(status_code=HTTP_403_FORBIDDEN, body=b"Forbidden")
        is_dir = stat_result is not None and stat.S_ISDIR(stat_result.st_mode)
        is_file = stat_result is not None and stat.S_ISREG(stat_result.st_mode)

        if not is_file:
            # Try the .html-suffixed variant when `html=True` is set
            # (handles `/about` -> `/about.html` mappings).
            if self.html and not relative.endswith(".html"):
                file_path_html = file_path + ".html"
                stat_html, denied_html = await loop.run_in_executor(None, _try_stat, file_path_html)
                if denied_html:
                    return Response(status_code=HTTP_403_FORBIDDEN, body=b"Forbidden")
                if stat_html is not None and stat.S_ISREG(stat_html.st_mode):
                    file_path = file_path_html
                    stat_result = stat_html
                    is_file = True
            if not is_file and self.directory_index and is_dir:
                # Symlink containment: same `commonpath` check as the file
                # path below - single rule for "real path stays under the
                # served root after symlink resolution" prevents a planted
                # symlink in the index path from escaping.
                real = await loop.run_in_executor(None, os.path.realpath, file_path)
                if not self._is_under_root(real):
                    return Response(status_code=HTTP_403_FORBIDDEN, body=b"Forbidden")
                return await self._render_directory_index(file_path, request.path, loop)
            if not is_file:
                return None

        # Symlink safety: `safe_join` blocks `..` traversal but does not
        # resolve symlinks. Dereference the real path and confirm it is
        # still inside the served root - a symlink planted in the served
        # directory must not expose files elsewhere on the filesystem.
        real_path = await loop.run_in_executor(None, os.path.realpath, file_path)
        if not self._is_under_root(real_path):
            return Response(status_code=HTTP_403_FORBIDDEN, body=b"Forbidden")

        # Derive the media type from the ORIGINAL path before any
        # precompressed swap, so `app.css.br` keeps `text/css` rather than
        # mislabelling as `application/gzip`.
        content_type = _guess_content_type(file_path)

        # Precompressed sibling serving (opt-in). On a hit, switch all
        # downstream bookkeeping (ETag, 304/412, Range, body) to the
        # compressed file so revalidation keys off the bytes actually sent.
        content_encoding: str | None = None
        try:
            variant = await self._select_precompressed(request, file_path, loop, _try_stat)
        except PermissionError:
            return Response(status_code=HTTP_403_FORBIDDEN, body=b"Forbidden")
        if variant is not None:
            variant_path, content_encoding, variant_stat = variant
            # A planted `.br`/`.gz` symlink must not escape the served root.
            variant_real = await loop.run_in_executor(None, os.path.realpath, variant_path)
            if not self._is_under_root(variant_real):
                return Response(status_code=HTTP_403_FORBIDDEN, body=b"Forbidden")
            file_path = variant_path
            stat_result = variant_stat

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

        # Write-side preconditions first (RFC 9110 Sec. 13.2.2): If-Match,
        # then If-Unmodified-Since, both ahead of the read-side 304 checks.
        # Veloce emits weak file ETags, so a concrete If-Match always fails
        # closed (only `*` succeeds); document this for static assets.
        if _precondition_failed(request.if_match, request.if_unmodified_since, etag, mtime):
            return Response(
                status_code=HTTP_412_PRECONDITION_FAILED,
                body=b"",
                headers={HEADER_ETAG: etag, HEADER_LAST_MODIFIED: last_modified},
            )

        # Conditional GET. Per RFC 9110 Sec. 13.2 precedence: If-None-Match
        # supersedes If-Modified-Since when both are present.
        if_none_match = request.headers.get(HEADER_IF_NONE_MATCH, "")
        if if_none_match:
            if if_none_match.strip() == "*":
                return Response(
                    status_code=HTTP_304_NOT_MODIFIED,
                    body=b"",
                    headers={HEADER_ETAG: etag, HEADER_LAST_MODIFIED: last_modified},
                )
            for token in if_none_match.split(","):
                if _etag_matches_weak(etag, token):
                    return Response(
                        status_code=HTTP_304_NOT_MODIFIED,
                        body=b"",
                        headers={HEADER_ETAG: etag, HEADER_LAST_MODIFIED: last_modified},
                    )
        else:
            ims_header = request.headers.get(HEADER_IF_MODIFIED_SINCE, "")
            ims_dt = parse_date(ims_header)
            ims_ts = ims_dt.timestamp() if ims_dt is not None else None
            # Floor mtime to whole seconds because HTTP-dates have second
            # resolution; otherwise `mtime=1.5` would always appear
            # "newer" than `IMS=1`.
            if ims_ts is not None and int(mtime) <= int(ims_ts):
                return Response(
                    status_code=HTTP_304_NOT_MODIFIED,
                    body=b"",
                    headers={HEADER_ETAG: etag, HEADER_LAST_MODIFIED: last_modified},
                )

        # Range request - RFC 9110 Sec. 14.2. Single-range only; multi-range
        # would require multipart/byteranges which we don't ship yet.
        range_spec = request.range
        # If-Range (RFC 9110 Sec. 13.1.5): honor the Range only when the
        # client's validator still matches the current representation.
        # Otherwise the resource changed since the client last saw it, and
        # serving a 206 slice would splice bytes from a different version into
        # a stale download - so fall through to a full 200 instead.
        honor_range = True
        if range_spec is not None:
            if_etag, if_date = request.if_range
            if if_etag:
                # RFC 9110 Sec. 13.1.5 mandates the STRONG comparison function
                # for an If-Range ETag: both tags must be strong (no `W/`
                # prefix) and byte-identical. A weak validator only guarantees
                # semantic equivalence, not the byte-for-byte identity a range
                # resume needs. Veloce emits weak file ETags, so an ETag
                # If-Range never satisfies strong comparison and we serve a
                # full 200 - clients should resume via the Last-Modified date.
                honor_range = (
                    not if_etag.startswith("W/") and not etag.startswith("W/") and if_etag == etag
                )
            elif if_date is not None:
                # RFC 9110 Sec. 13.1.5 requires an EXACT date match here (unlike
                # the "earlier than or equal" test used for If-Unmodified-Since).
                # Compare at HTTP-date (whole-second) resolution.
                honor_range = int(mtime) == int(if_date)
        if (
            honor_range
            and range_spec is not None
            and range_spec.unit == HEADER_VALUE_BYTES
            and len(range_spec.ranges) == 1
        ):
            start, end = range_spec.ranges[0]
            if start is None and end is None:
                resolved = None
            elif start is None:
                # Suffix range: last `end` bytes. `bytes=-500` over a 200-byte
                # file should return the whole file, per RFC 9110 Sec. 14.1.2.
                suffix = min(end or 0, size)
                resolved = (size - suffix, size - 1) if suffix > 0 else None
            else:
                resolved = (start, end if end is not None and end < size else size - 1)

            if resolved is None or resolved[0] >= size or resolved[0] > resolved[1]:
                return Response(
                    status_code=HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                    body=b"",
                    headers={
                        HEADER_CONTENT_RANGE: f"bytes */{size}",
                        HEADER_ETAG: etag,
                        HEADER_LAST_MODIFIED: last_modified,
                        HEADER_ACCEPT_RANGES: HEADER_VALUE_BYTES,
                    },
                )

            r_start, r_end = resolved
            length = r_end - r_start + 1

            def _read_range() -> bytes:
                with open(file_path, "rb") as f:
                    f.seek(r_start)
                    return f.read(length)

            body = await loop.run_in_executor(None, _read_range)
            range_headers = {
                HEADER_CONTENT_RANGE: f"bytes {r_start}-{r_end}/{size}",
                HEADER_ETAG: etag,
                HEADER_LAST_MODIFIED: last_modified,
                HEADER_ACCEPT_RANGES: HEADER_VALUE_BYTES,
                HEADER_CACHE_CONTROL: "public, max-age=3600",
            }
            if content_encoding is not None:
                # The range is over the compressed bytes; advertise the
                # encoding so a shared cache never serves these bytes to a
                # client that did not ask for them.
                range_headers[HEADER_CONTENT_ENCODING] = content_encoding
            if self.precompressed:
                # The asset is content-negotiated on Accept-Encoding, so even
                # the identity slice (client sent no / `q=0` Accept-Encoding)
                # must carry `Vary: Accept-Encoding` - otherwise a shared cache
                # may replay this uncompressed range to a compression-capable
                # client (RFC 9110 Sec. 12.5.5).
                range_headers[HEADER_VARY] = HEADER_ACCEPT_ENCODING
            return Response(
                status_code=HTTP_206_PARTIAL_CONTENT,
                body=body,
                content_type=content_type,
                headers=range_headers,
            )

        common_headers = {
            HEADER_ETAG: etag,
            HEADER_LAST_MODIFIED: last_modified,
            HEADER_ACCEPT_RANGES: HEADER_VALUE_BYTES,
            HEADER_CACHE_CONTROL: "public, max-age=3600",
        }
        if content_encoding is not None:
            common_headers[HEADER_CONTENT_ENCODING] = content_encoding
        if self.precompressed:
            # `Vary: Accept-Encoding` on every response for a precompressed-
            # enabled asset - including the identity body served when the
            # client sent no acceptable encoding - so a shared cache keys this
            # uncompressed representation separately from the br/gz variants
            # and never replays it to a compression-capable client
            # (RFC 9110 Sec. 12.5.5).
            common_headers[HEADER_VARY] = HEADER_ACCEPT_ENCODING

        # Files at or above `STREAM_THRESHOLD` use chunked streaming so
        # the whole file never sits in memory at once - a single large
        # download (or many concurrent ones) no longer balloons the
        # worker's RSS by the file size. Smaller files stay buffered
        # because (a) the response is one ASGI message instead of N,
        # and (b) the per-chunk syscall overhead dominates at small
        # sizes. Range responses always buffer their slice - a range is
        # already bounded by the client.
        if size >= self.STREAM_THRESHOLD:
            # Don't emit `Content-Length` alongside chunked transfer -
            # RFC 9112 Sec. 6.1 forbids carrying both, and a strict proxy
            # may drop or 502 the response. Clients that need a
            # progress hint can issue a HEAD or read `ETag`.
            return StreamingResponse(
                content=self._iter_file(file_path, loop),
                status_code=HTTP_200_OK,
                content_type=content_type,
                headers=dict(common_headers),
            )

        def _read() -> bytes:
            with open(file_path, "rb") as f:
                return f.read()

        body = await loop.run_in_executor(None, _read)

        return Response(
            status_code=HTTP_200_OK,
            body=body,
            content_type=content_type,
            headers=common_headers,
        )

    async def _iter_file(self, path: str, loop: Any) -> AsyncIterator[bytes]:
        """Yield the file in `STREAM_CHUNK_SIZE`-byte chunks via the executor.

        The file handle is opened on the executor (blocking syscall) and
        closed in a finally so a client disconnect mid-stream doesn't
        leak a descriptor. Each `read` runs on the executor too - the
        event loop stays responsive while a slow disk delivers bytes.
        """

        def _open() -> Any:
            return open(path, "rb")  # noqa: SIM115 - closed in finally

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
        """Compute ETag - delegates to the shared `_file_etag` helper so the
        StaticFiles handler and `FileResponse` validate against the same
        `If-None-Match` value for the same file."""

        return _file_etag(path, size, mtime)

    async def _render_directory_index(self, dir_path: str, url_path: str, loop: Any) -> Response:
        """Render an HTML index of `dir_path`'s entries.

        Entries are HTML-escaped via `html.escape` so a filename
        containing `<script>` can't poison the page. Subdirectories
        get a trailing slash. Hidden files (`.foo`) are omitted -
        matches nginx `autoindex on;` default.
        """

        def _list_dir() -> list[tuple[str, bool]]:
            """Return `(name, is_dir)` tuples for the directory.

            `os.scandir` answers `is_dir()` from cached stat data on the
            same syscall that produced the entry, so we don't need a
            second `os.path.isdir` per item.
            """
            out: list[tuple[str, bool]] = []
            try:
                with os.scandir(dir_path) as it:
                    for e in it:
                        if e.name.startswith("."):
                            continue
                        # Per-entry symlink containment. A symlink whose target
                        # resolves OUTSIDE the served root is dropped so the
                        # index never leaks out-of-root names, mirroring the
                        # per-file 403 at handle(). realpath is best-effort
                        # (never raises; broken/escaping links resolve outside
                        # root) and commonpath in _is_under_root rejects them.
                        # is_symlink() short-circuits so non-links pay no cost.
                        if e.is_symlink() and not self._is_under_root(os.path.realpath(e.path)):
                            continue
                        out.append((e.name, e.is_dir(follow_symlinks=False)))
            except OSError:
                return []
            out.sort(key=lambda t: t[0])
            return out

        entries = await loop.run_in_executor(None, _list_dir)
        base = url_path if url_path.endswith("/") else url_path + "/"

        rows: list[str] = []
        # Parent-directory link unless we're at the prefix root.
        if base.rstrip("/") != self.prefix:
            rows.append('<li><a href="../">../</a></li>')

        for name, is_dir in entries:
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
            status_code=HTTP_200_OK,
            body=body.encode("utf-8"),
            content_type=MIME_TEXT_HTML_UTF8,
        )
