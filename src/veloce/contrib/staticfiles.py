"""Static files — file serving with caching, ETag, and Last-Modified support.

All file I/O runs in the executor so the event loop is never blocked.
Conditional GET responses follow RFC 9110 Sec. 13.1.

Spec anchors:
- RFC 9110 Sec. 8.8.2 — Last-Modified header
- RFC 9110 Sec. 8.8.3 — ETag header
- RFC 9110 Sec. 13.1.1 — If-Match
- RFC 9110 Sec. 13.1.3 — If-Modified-Since
- RFC 9110 Sec. 13.1.4 — If-None-Match
- RFC 9110 Sec. 13.1.4 — If-Unmodified-Since
- RFC 9110 Sec. 13.2.2 — precondition precedence
"""

from __future__ import annotations

import asyncio
import html
import os
import stat
import warnings
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

from veloce._constants import (
    HEADER_ACCEPT_ENCODING,
    HEADER_ACCEPT_RANGES,
    HEADER_CACHE_CONTROL,
    HEADER_CONTENT_ENCODING,
    HEADER_CONTENT_RANGE,
    HEADER_ETAG,
    HEADER_LAST_MODIFIED,
    HEADER_VALUE_BYTES,
    HEADER_VARY,
    MIME_TEXT_HTML_UTF8,
)
from veloce._internal import (
    _etag_matches_strong,
    _file_etag,
    _preconditions_say_unchanged,
    guess_content_type,
)
from veloce._protocol_constants import HTTP_METHOD_GET
from veloce.http.dates import http_date
from veloce.http.request import Request
from veloce.http.response import RedirectResponse, Response, StreamingResponse
from veloce.safe import safe_join
from veloce.status import (
    HTTP_200_OK,
    HTTP_206_PARTIAL_CONTENT,
    HTTP_304_NOT_MODIFIED,
    HTTP_307_TEMPORARY_REDIRECT,
    HTTP_308_PERMANENT_REDIRECT,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_406_NOT_ACCEPTABLE,
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
        return all(not _etag_matches_strong(etag, token) for token in if_match)
    if if_unmodified_since is not None:
        # HTTP-dates carry second resolution - floor mtime to compare.
        return int(mtime) > int(if_unmodified_since)
    return False


async def _stat_path(loop: asyncio.AbstractEventLoop, path: str) -> os.stat_result | None:
    """`os.stat(path)` off the event loop, or `None` when it does not exist.

    Raises `PermissionError` when the filesystem refuses the probe, so a denial
    reaches `handle`'s single `except` and becomes a `403` rather than bubbling
    to a `500`.
    """

    def _probe() -> os.stat_result | None:
        try:
            return os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None

    result: os.stat_result | None = await loop.run_in_executor(None, _probe)
    return result


async def _stat_regular(loop: asyncio.AbstractEventLoop, path: str) -> os.stat_result | None:
    """As `_stat_path`, but `None` unless `path` is a regular file."""
    result = await _stat_path(loop, path)
    return result if result is not None and stat.S_ISREG(result.st_mode) else None


def _forbidden() -> Response:
    """Build the canonical 403 served on traversal / symlink-escape / EACCES.

    Every static denial returns the same opaque body so the handler never
    discloses whether a path exists, only that it may not be read.
    """
    return Response(status_code=HTTP_403_FORBIDDEN, body=b"Forbidden")


def _not_modified(etag: str, last_modified: str) -> Response:
    """Build the 304 carrying the current validators (RFC 9110 Sec. 13.1.4).

    The empty-body 304 must still echo `ETag` and `Last-Modified` so the
    client can refresh its cached validators on the revalidation hit.
    """
    return Response(
        status_code=HTTP_304_NOT_MODIFIED,
        body=b"",
        headers={HEADER_ETAG: etag, HEADER_LAST_MODIFIED: last_modified},
    )


#: Sent when neither the handler nor the app configures a cache lifetime.
_DEFAULT_CACHE_CONTROL = "public, max-age=3600"


def _conditional_hit(request: Request, etag: str, mtime: float) -> bool:
    """Whether the request's preconditions say the client's copy is current.

    RFC 9110 Sec. 13.2 precedence: `If-None-Match` supersedes `If-Modified-Since`
    when both are present. `*` matches whenever a representation exists, wherever
    it appears in the list (RFC 9110 Sec. 13.1.2).

    Reads the parsed property, which - unlike `split(",")` - does not break on
    a comma inside an opaque tag's quoted string, and is cached per request.
    """
    return _preconditions_say_unchanged(
        request.if_none_match, request.if_modified_since, etag, mtime
    )


def _resolve_range(first: tuple[int | None, int | None], size: int) -> tuple[int, int] | None:
    """Turn one parsed `bytes=` range into absolute offsets, or `None` if unusable.

    `None` covers both an open-open range and a suffix range of zero bytes; the
    caller answers those with a 416, as it does an out-of-bounds result.
    """
    start, end = first
    if start is None and end is None:
        return None
    if start is None:
        # Suffix range: last `end` bytes. `bytes=-500` over a 200-byte file
        # should return the whole file, per RFC 9110 Sec. 14.1.2.
        suffix = min(end or 0, size)
        return (size - suffix, size - 1) if suffix > 0 else None
    return (start, end if end is not None and end < size else size - 1)


class StaticFiles:
    """Serve static files from a directory — all file I/O runs in executor.

    Usage::

        from veloce import Veloce
        from veloce.contrib.staticfiles import StaticFiles

        app = Veloce()
        app.mount("/static", StaticFiles(directory="static"))
    """

    # Without a cap, every served path lives in the dict forever.
    ETAG_CACHE_MAX = 1024
    # Streamed at or above this size: smaller files keep the single-message ASGI
    # fast path, larger ones never sit whole in worker RSS. Tune per instance.
    STREAM_THRESHOLD = 1 * 1024 * 1024
    # 64 KiB matches asyncio's default transport write-buffer high-water mark,
    # so chunks ride the wire without rebuffering.
    STREAM_CHUNK_SIZE = 64 * 1024
    # Content-Encoding token -> sibling suffix. The order only breaks q-value
    # ties, `br` first as the denser codec; selection follows Accept-Encoding.
    PRECOMPRESSED_VARIANTS = {"br": ".br", "gzip": ".gz"}

    def __init__(
        self,
        directory: str,
        prefix: str = "/static",
        html: bool = False,
        directory_index: bool = False,
        must_exist: bool = True,
        precompressed: bool = False,
        redirect_status: int = HTTP_307_TEMPORARY_REDIRECT,
        max_age: int | None = None,
    ) -> None:
        self.directory = os.path.abspath(directory)
        # Resolved once: it cannot change for the life of the handler.
        self.max_age = max_age
        self._cache_control = f"public, max-age={max_age}" if max_age is not None else None
        # Why the directory could not be served, or `None`; always set.
        self._setup_problem: str | None = None
        # Checked at construction, not on the async request path: a typo
        # otherwise builds a handler that silently 404s every asset.
        if not os.path.isdir(self.directory):
            problem = (
                f"StaticFiles directory {directory!r} (resolved to "
                f"{self.directory!r}) does not exist or is not a directory"
            )
            if must_exist:
                raise ValueError(problem)
            warnings.warn(problem, stacklevel=2)
            self._setup_problem = problem
        elif not os.access(self.directory, (os.R_OK | os.X_OK) if directory_index else os.X_OK):
            # Serving a file needs SEARCH (X_OK); a listing also needs READ
            # (R_OK). Require exactly what `handle()` will use.
            need = "readable and searchable" if directory_index else "searchable"
            problem = (
                f"StaticFiles directory {directory!r} (resolved to "
                f"{self.directory!r}) exists but is not {need}"
            )
            if must_exist:
                raise ValueError(problem)
            warnings.warn(problem, stacklevel=2)
            self._setup_problem = problem
        # Symlink-resolved once; the containment check runs per request.
        self._real_root = os.path.realpath(self.directory)
        self.prefix = prefix.rstrip("/")
        self.html = html
        # Off by default: a directory listing is an information-disclosure risk.
        self.directory_index = directory_index
        # Off by default: the variants must be pre-generated, and the feature
        # costs one extra `stat` per request when enabled.
        self.precompressed = precompressed
        # Only 307/308: a 301/302 would change the method on a GET asset path.
        if redirect_status not in (HTTP_307_TEMPORARY_REDIRECT, HTTP_308_PERMANENT_REDIRECT):
            raise ValueError(
                f"StaticFiles redirect_status must be 307 or 308, got {redirect_status!r}"
            )
        self.redirect_status = redirect_status
        # Bounded LRU: insertion order doubles as recency.
        self._etag_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def _is_under_root(self, real_path: str) -> bool:
        """True when `real_path` (already realpath-resolved) is inside the root.

        Uses `commonpath` so prefix-collisions (e.g. `/srv/static_evil/...`
        against root `/srv/static`) are correctly rejected. `ValueError` on
        Windows drive mismatches counts as out.
        """
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

    def _redirect_to_slash(self, request: Request) -> RedirectResponse:
        """Redirect a slash-less directory URL to its slash-terminated form.

        The query string is preserved so `?v=2` survives the redirect. Only the
        path gains a trailing slash; `RedirectResponse` percent-encodes and
        CRLF-rejects the Location for us.
        """
        target = request.path + "/"
        if request.query_string:
            target = f"{target}?{request.query_string}"
        return RedirectResponse(url=target, status_code=self.redirect_status)

    async def _serve_404_html(self, loop: asyncio.AbstractEventLoop) -> Response | None:
        """Return a 404 response from the root `404.html`, or None if absent.

        The page is served with status 404 and `text/html`; it deliberately
        skips the ETag/range/conditional machinery (an error body does not need
        a validator). Symlink containment is enforced so a planted `404.html`
        link cannot read a file outside the served root. Permission errors on
        the page surface as 403, matching the read policy on any served file.
        """
        page_path = safe_join(self.directory, "404.html")
        if page_path is None:
            return None
        if await _stat_regular(loop, page_path) is None:
            return None
        real = await loop.run_in_executor(None, os.path.realpath, page_path)
        if not self._is_under_root(real):
            return _forbidden()

        def _read() -> bytes:
            with open(page_path, "rb") as f:
                return f.read()

        body = await loop.run_in_executor(None, _read)
        return Response(
            status_code=HTTP_404_NOT_FOUND,
            body=body,
            content_type=MIME_TEXT_HTML_UTF8,
        )

    async def _select_precompressed(
        self,
        request: Request,
        file_path: str,
        loop: asyncio.AbstractEventLoop,
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
        Permission errors on the sibling propagate as 403, matching the read
        policy on the original file.
        """
        if not self.precompressed:
            return None
        # The negated index keeps the sort stable on the table's tie order.
        accept = request.accept_encodings
        order = list(self.PRECOMPRESSED_VARIANTS)
        # RFC 9110 Sec. 12.5.3: an explicit `q=0` rejection must override a
        # permissive `*`. `quality()` returns the MAX across both, so
        # `br;q=0, *;q=1` would score br at 1.0; `quality_explicit()` does not.
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
            variant_stat = await _stat_regular(loop, variant_path)
            if variant_stat is not None:
                return (variant_path, enc, variant_stat)
        return None

    def _cache_control_for(self, request: Request) -> str:
        """Choose the `Cache-Control` to send, from the handler or the app default.

        An explicit `max_age=` on the handler wins; otherwise
        `SEND_FILE_MAX_AGE_DEFAULT`, which `send_file` also honours.
        """
        if self._cache_control is not None:
            return self._cache_control
        app = getattr(request, "app", None)
        config = getattr(app, "config", None) if app is not None else None
        configured = config.get("SEND_FILE_MAX_AGE_DEFAULT") if config else None
        if configured is None:
            return _DEFAULT_CACHE_CONTROL
        return f"public, max-age={configured}"

    def audit(self, ctx: Any) -> Any:
        """Report a served root the constructor could only warn about.

        `must_exist=False` downgrades a missing or unreadable directory to a
        `warnings.warn` at construction, which never reaches `veloce check`.
        Informational: the dev flow that creates the directory after wiring the
        app is the reason the downgrade exists.
        """
        problem = self._setup_problem
        if problem is None:
            return ()
        from veloce.audit import Finding

        return (
            Finding(
                f"{problem}, so every asset under {self.prefix!r} will 404.",
                severity="info",
                fix="create the directory, or correct the path passed to StaticFiles",
                id="static-directory-missing",
            ),
        )

    async def handle(self, request: Request) -> Response | None:
        """Serve one static file, from resolving the target to emitting the body.

        Eight jobs in sequence, each marked with a rule below. Every one after
        the first reads what the ones before it settled - the resolved path, the
        stat, the chosen variant, the ETag - so it stays one sequence rather
        than eight methods threading six accumulating values through eight
        signatures, on the path every static request takes.

        All filesystem work is offloaded to the thread pool.
        """
        # A probe the filesystem refuses is a 403, not a 500, at every point
        # this method probes - so it is caught once, here.
        try:
            path = request.path
            if not path.startswith(self.prefix):
                return None

            relative = path[len(self.prefix) :].lstrip("/")
            if not relative and self.html:
                relative = "index.html"

            # ── Resolve the target, traversal-safe ────────────────────
            # `safe_join` rejects `..`, absolute components and NUL bytes,
            # returning None on any escape attempt. Pure string arithmetic.
            file_path = safe_join(self.directory, relative)  # noqa: ASYNC240
            if file_path is None:
                return _forbidden()

            loop = asyncio.get_running_loop()

            # One stat: `S_ISREG`/`S_ISDIR` tell file from dir without a second
            # syscall, and a `PermissionError` reaches the 403 above.
            stat_result = await _stat_path(loop, file_path)
            is_dir = stat_result is not None and stat.S_ISDIR(stat_result.st_mode)
            is_file = stat_result is not None and stat.S_ISREG(stat_result.st_mode)

            if not is_file:
                # `/about` -> `/about.html`.
                if self.html and not relative.endswith(".html"):
                    file_path_html = file_path + ".html"
                    stat_html = await _stat_regular(loop, file_path_html)
                    if stat_html is not None:
                        file_path = file_path_html
                        stat_result = stat_html
                        is_file = True
                # A slash-less directory URL (`/s/docs`) redirects to the
                # slash-terminated form first, so the browser resolves the
                # page's relative links against the directory, not its parent.
                if not is_file and self.html and is_dir:
                    index_path = os.path.join(file_path, "index.html")
                    stat_index = await _stat_regular(loop, index_path)
                    if stat_index is not None:
                        if not request.path.endswith("/"):
                            return self._redirect_to_slash(request)
                        file_path = index_path
                        stat_result = stat_index
                        is_file = True
                if not is_file and self.directory_index and is_dir:
                    # Symlink containment: a planted symlink in the index path
                    # must not resolve outside the served root.
                    real = await loop.run_in_executor(None, os.path.realpath, file_path)
                    if not self._is_under_root(real):
                        return _forbidden()
                    return await self._render_directory_index(file_path, request.path, loop)
                # Custom not-found page, symlink-contained in the helper.
                if not is_file and self.html:
                    not_found = await self._serve_404_html(loop)
                    if not_found is not None:
                        return not_found
                if not is_file:
                    return None

            # ── Symlink containment ───────────────────────────────────
            # `safe_join` blocks `..` but does not resolve symlinks: a symlink
            # planted in the served directory must not expose files elsewhere.
            real_path = await loop.run_in_executor(None, os.path.realpath, file_path)
            if not self._is_under_root(real_path):
                return _forbidden()

            # From the ORIGINAL path, before any precompressed swap, so
            # `app.css.br` keeps `text/css` rather than `application/gzip`.
            content_type = guess_content_type(file_path)

            # On a hit, all downstream bookkeeping (ETag, 304/412, Range, body)
            # switches to the compressed file, so revalidation keys off the bytes
            # actually sent.
            content_encoding: str | None = None
            # ── Precompressed negotiation ─────────────────────────────
            variant = await self._select_precompressed(request, file_path, loop)
            if variant is not None:
                variant_path, content_encoding, variant_stat = variant
                # A planted `.br`/`.gz` symlink must not escape the served root.
                variant_real = await loop.run_in_executor(None, os.path.realpath, variant_path)
                if not self._is_under_root(variant_real):
                    return _forbidden()
                file_path = variant_path
                stat_result = variant_stat
            elif self.precompressed and not request.accept_encodings.accepts_identity():
                # No sibling, and the client rejected the identity coding
                # (`Accept-Encoding: identity;q=0, br;q=0, gzip;q=0`). RFC 9110
                # Sec. 12.5.3: the raw asset would be an unacceptable coding.
                return Response(status_code=HTTP_406_NOT_ACCEPTABLE, body=b"Not Acceptable")

            # Populated by the existence check above, which narrowed it.
            assert stat_result is not None
            mtime = stat_result.st_mtime
            size = stat_result.st_size
            cache_key = file_path

            # A stale entry and a missing one are the same case.
            cached = self._etag_cache.get(cache_key)
            if cached is not None and cached[1] == mtime:
                etag = cached[0]
                self._etag_cache.move_to_end(cache_key)
            else:
                etag = self._compute_etag(file_path, size, mtime)
                self._remember_etag(cache_key, etag, mtime)

            last_modified = http_date(mtime)

            # ── Write-side preconditions (RFC 9110 Sec. 13.2.2) ───────
            # Both ahead of the read-side 304 checks.
            if _precondition_failed(request.if_match, request.if_unmodified_since, etag, mtime):
                return Response(
                    status_code=HTTP_412_PRECONDITION_FAILED,
                    body=b"",
                    headers={HEADER_ETAG: etag, HEADER_LAST_MODIFIED: last_modified},
                )

            # ── Conditional GET (RFC 9110 Sec. 13.2) ──────────────────
            if _conditional_hit(request, etag, mtime):
                return _not_modified(etag, last_modified)

            # ── Range (RFC 9110 Sec. 14.2) ────────────────────────────
            # Single-range only; multi-range needs multipart/byteranges. `GET`
            # is the only method range handling is defined for, `HEAD` included:
            # `Accept-Ranges` already answers a range probe (Sec. 14.3), while a
            # 416 would deny a download manager the full `Content-Length`.
            range_spec = request.range if request.method == HTTP_METHOD_GET else None
            # If-Range (RFC 9110 Sec. 13.1.5): honor the Range only while the
            # client's validator matches, or a 206 slice splices bytes from a
            # different version into a stale download.
            honor_range = True
            if range_spec is not None:
                if_etag, if_date = request.if_range
                if if_etag:
                    # RFC 9110 Sec. 13.1.5 mandates the STRONG comparison
                    # (Sec. 8.8.3.1): a weak validator guarantees semantic
                    # equivalence, not the byte identity a resume needs - so the
                    # stock weak file ETag never resumes here.
                    honor_range = _etag_matches_strong(etag, if_etag)
                elif if_date is not None:
                    # RFC 9110 Sec. 13.1.5 requires an EXACT date match here,
                    # at HTTP-date (whole-second) resolution.
                    honor_range = int(mtime) == int(if_date)
            if (
                honor_range
                and range_spec is not None
                and range_spec.unit == HEADER_VALUE_BYTES
                and len(range_spec.ranges) == 1
            ):
                resolved = _resolve_range(range_spec.ranges[0], size)

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
                range_headers = {
                    HEADER_CONTENT_RANGE: f"bytes {r_start}-{r_end}/{size}",
                    HEADER_ETAG: etag,
                    HEADER_LAST_MODIFIED: last_modified,
                    HEADER_ACCEPT_RANGES: HEADER_VALUE_BYTES,
                    HEADER_CACHE_CONTROL: self._cache_control_for(request),
                }
                if content_encoding is not None:
                    # The range is over the compressed bytes, so a shared cache
                    # must not serve them to a client that did not ask.
                    range_headers[HEADER_CONTENT_ENCODING] = content_encoding
                if self.precompressed:
                    # Even the identity slice must carry `Vary: Accept-Encoding`,
                    # or a shared cache may replay this uncompressed range to a
                    # compression-capable client (RFC 9110 Sec. 12.5.5).
                    range_headers[HEADER_VARY] = HEADER_ACCEPT_ENCODING
                # The threshold applies to the resolved slice, since
                # `Range: bytes=0-` is a well-formed range over the whole file.
                if length >= self.STREAM_THRESHOLD:
                    return StreamingResponse(
                        content=self._iter_file(file_path, loop, r_start, length),
                        status_code=HTTP_206_PARTIAL_CONTENT,
                        content_type=content_type,
                        headers=range_headers,
                    )

                def _read_range() -> bytes:
                    with open(file_path, "rb") as f:
                        f.seek(r_start)
                        return f.read(length)

                return Response(
                    status_code=HTTP_206_PARTIAL_CONTENT,
                    body=await loop.run_in_executor(None, _read_range),
                    content_type=content_type,
                    headers=range_headers,
                )

            common_headers = {
                HEADER_ETAG: etag,
                HEADER_LAST_MODIFIED: last_modified,
                HEADER_ACCEPT_RANGES: HEADER_VALUE_BYTES,
                HEADER_CACHE_CONTROL: self._cache_control_for(request),
            }
            if content_encoding is not None:
                common_headers[HEADER_CONTENT_ENCODING] = content_encoding
            if self.precompressed:
                # On every response, the identity body included, so a shared cache
                # keys it separately from the br/gz variants (RFC 9110 Sec. 12.5.5).
                common_headers[HEADER_VARY] = HEADER_ACCEPT_ENCODING

            if size >= self.STREAM_THRESHOLD:
                # No `Content-Length` alongside chunked transfer: RFC 9112
                # Sec. 6.1 forbids both, and a strict proxy may 502 it.
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
        except PermissionError:
            return _forbidden()

    async def _iter_file(
        self, path: str, loop: asyncio.AbstractEventLoop, start: int = 0, length: int | None = None
    ) -> AsyncIterator[bytes]:
        """Yield the file in `STREAM_CHUNK_SIZE`-byte chunks via the executor.

        `start` and `length` bound the read to a byte range; the defaults cover
        the whole file.

        The handle is opened on the executor and closed in a `finally`, so a
        client disconnect mid-stream does not leak a descriptor.
        """

        def _open() -> Any:
            return open(path, "rb")  # noqa: SIM115 - closed in finally

        chunk_size = self.STREAM_CHUNK_SIZE
        remaining = length
        fh = await loop.run_in_executor(None, _open)
        try:
            if start:
                await loop.run_in_executor(None, fh.seek, start)
            while remaining is None or remaining > 0:
                want = chunk_size if remaining is None else min(chunk_size, remaining)
                chunk = await loop.run_in_executor(None, fh.read, want)
                if not chunk:
                    break
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk
        finally:
            await loop.run_in_executor(None, fh.close)

    def _compute_etag(self, path: str, size: int, mtime: float) -> str:
        """Compute the file's ETag through the shared `_file_etag` helper.

        The `StaticFiles` handler and `FileResponse` validate against the same
        `If-None-Match` value for the same file.
        """
        return _file_etag(path, size, mtime)

    async def _render_directory_index(
        self, dir_path: str, url_path: str, loop: asyncio.AbstractEventLoop
    ) -> Response:
        """Render an HTML index of `dir_path`'s entries.

        Entries are HTML-escaped via `html.escape` so a filename
        containing `<script>` can't poison the page. Subdirectories
        get a trailing slash. Hidden files (`.foo`) are omitted -
        matches nginx `autoindex on;` default.
        """

        def _list_dir() -> list[tuple[str, bool]]:
            """Return `(name, is_dir)` tuples for the directory.

            `os.scandir` answers `is_dir()` from stat data cached by the same
            syscall that produced the entry, so no `os.path.isdir` per item.
            """
            out: list[tuple[str, bool]] = []
            try:
                with os.scandir(dir_path) as it:
                    for e in it:
                        if e.name.startswith("."):
                            continue
                        # Per-entry symlink containment: a link resolving
                        # outside the served root is dropped so the index never
                        # leaks out-of-root names. `is_symlink()` short-circuits.
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
