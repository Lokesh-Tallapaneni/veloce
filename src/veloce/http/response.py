"""Response types — optimized serialization with orjson."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, BinaryIO, Literal
from urllib.parse import quote

import orjson

from veloce._constants import (
    HEADER_ACCEPT_RANGES,
    HEADER_AGE,
    HEADER_ALLOW,
    HEADER_CACHE_CONTROL,
    HEADER_CONTENT_DISPOSITION,
    HEADER_CONTENT_ENCODING,
    HEADER_CONTENT_LANGUAGE,
    HEADER_CONTENT_LENGTH,
    HEADER_CONTENT_LOCATION,
    HEADER_CONTENT_RANGE,
    HEADER_CONTENT_TYPE,
    HEADER_DATE,
    HEADER_ETAG,
    HEADER_EXPIRES,
    HEADER_LAST_MODIFIED,
    HEADER_LOCATION,
    HEADER_RETRY_AFTER,
    HEADER_SET_COOKIE,
    HEADER_TRANSFER_ENCODING,
    HEADER_VALUE_ATTACHMENT,
    HEADER_VALUE_BYTES,
    HEADER_VALUE_CHUNKED,
    HEADER_VALUE_NO_CACHE,
    HEADER_VALUE_PUBLIC,
    HEADER_VARY,
    HEADER_WWW_AUTHENTICATE,
    MIME_TEXT_PLAIN,
    MSG_LABEL_HEADER_NAME,
    MSG_LABEL_SET_COOKIE_VALUE,
)
from veloce._header_parsing import parse_media_type_params
from veloce._internal import (
    _STATUS_PHRASES,
    MIME_HTML,
    MIME_JSON,
    MIME_OCTET,
    MIME_PLAIN,
    _encode_response_head,
    _etag_matches_strong,
    _file_etag,
    _header_value_has_crlf,
    _preconditions_say_unchanged,
    _quote_header_value,
    _reject_header_crlf,
    _write_chunked,
    dumps_current,
    guess_content_type,
    is_json_mimetype,
)
from veloce._protocol_constants import AUTH_SCHEME_BASIC, SET_COOKIE_JOINER
from veloce._warnings import VeloceDeprecationWarning
from veloce.http.cache_control import CacheControl
from veloce.http.cookies import dump_cookie, iter_cookies
from veloce.http.dates import http_date, parse_date
from veloce.http.header_set import HeaderSet
from veloce.status import (
    HTTP_200_OK,
    HTTP_304_NOT_MODIFIED,
    HTTP_307_TEMPORARY_REDIRECT,
    status_permits_body,
)

# ── Case-insensitive response-header access ──────────────────────────
# `Response.headers` is a plain `dict` (case-SENSITIVE), not a CIMultiDict.
# HTTP field names are case-insensitive (RFC 9110 Sec. 5.1), so a handler or
# upstream middleware that sets `cache-control`, `Etag`, or any other header
# in non-canonical casing would be missed by an exact-key lookup. These
# helpers fold the lookup case-insensitively without allocating a new dict on
# the hot path - they fast-path the canonical key, then fall back to a single
# linear scan only when it is absent.


def header_key(headers: Mapping[str, str], name: str) -> str | None:
    """Return the actual stored key matching `name` case-insensitively, or None.

    `name` should be passed in its canonical casing; the common case (the
    header is stored under that exact key) returns without scanning. Use the
    returned key to rewrite a value in place under whatever casing the caller
    originally stored.
    """
    if name in headers:
        return name
    lowered = name.lower()
    # A first-byte guard before the `.lower()` was measured slower here: the
    # header dict is short enough that the extra test costs more than the
    # allocations it avoids.
    for key in headers:
        if key.lower() == lowered:
            return key
    return None


def header_get(headers: Mapping[str, str], name: str) -> str | None:
    """Return the value stored under `name` case-insensitively, or None."""
    key = header_key(headers, name)
    return None if key is None else headers[key]


def header_present(headers: Mapping[str, str], name: str) -> bool:
    """Return True when a header named `name` exists under any casing."""
    return header_key(headers, name) is not None


def header_pop(headers: MutableMapping[str, str], name: str) -> str | None:
    """Remove and return `name` under whatever casing it was stored, or None.

    The replacement half of `header_get`. Every site that rewrites a header
    hand-rolled this, and each covered only the casings its author thought of -
    the canonical one and, sometimes, the lower-case one. A contribution written
    under any other casing was left in place, so `Vary`, `Allow` and
    `Content-Length` could each be emitted twice, and CORS silently discarded an
    `Access-Control-Expose-Headers` entry another middleware had added.

    Costs one dict lookup when the header is stored under its canonical casing,
    which is what the framework itself always writes.
    """
    key = header_key(headers, name)
    return None if key is None else headers.pop(key)


def _format_content_disposition(disposition: str, filename: str) -> str:
    """Build a safe RFC 6266 ``Content-Disposition`` header value.

    An ASCII name whose characters are all RFC 9110 quoted-string members
    (HTAB, SP, and the printable range 0x21-0x7E) is emitted verbatim as
    ``filename="..."`` with spaces and punctuation preserved - only ``\\``
    and ``"`` are escaped (backslash first). A non-ASCII name, or one that
    holds a control character, is emitted only as the RFC 5987
    ``filename*=UTF-8''...`` form; there is no lossy legacy ``filename=``
    slot. A CR/LF in the name is rejected outright so it cannot inject a
    header.
    """
    _reject_header_crlf(filename, "filename")
    quotable = filename.isascii() and all(
        c == "\t" or c == " " or "\x21" <= c <= "\x7e" for c in filename
    )
    if quotable:
        escaped = _quote_header_value(filename)
        param = f'filename="{escaped}"'
    else:
        param = f"filename*=UTF-8''{quote(filename, safe='')}"
    return f"{disposition}; {param}"


async def _stream_file(path: str, loop: Any) -> Any:
    """Yield a file's bytes in chunks, each read in the executor.

    The open and every read are offloaded, so no disk I/O runs on the event
    loop, and only one chunk is resident at a time.
    """
    handle = await loop.run_in_executor(None, _open_file_binary, path)
    try:
        while True:
            chunk = await loop.run_in_executor(None, _read_file_chunk, handle)
            if not chunk:
                return
            yield chunk
    finally:
        await loop.run_in_executor(None, handle.close)


def _open_file_binary(path: str) -> BinaryIO:
    """Open `path` for binary reading - run in an executor."""
    return open(path, "rb")  # noqa: SIM115 - closed by `_stream_file`


#: Bytes per chunk when streaming a file off disk. Large enough that the
#: per-chunk executor hop is amortised, small enough that a concurrent download
#: holds this rather than the whole file.
FILE_STREAM_CHUNK = 64 * 1024


def _read_file_chunk(handle: BinaryIO) -> bytes:
    """Read one chunk from an open file - run in an executor."""
    return handle.read(FILE_STREAM_CHUNK)


# Merged `Vary` values, keyed by `(existing_header, names_being_added)`. See
# `Response.add_vary`: the merge is pure, and a given application asks the same
# handful of questions on every response because its middleware order is fixed.
_VARY_MERGES: dict[tuple[str, tuple[str, ...]], str] = {}
_MAX_VARY_MERGES = 256


class Response:
    """Base HTTP response.

    Usage::

        from veloce import Response

        async def handler(request):
            return Response(body=b"hello", content_type="text/plain")
    """

    __slots__ = (
        "status_code",
        "_body",
        "content_type",
        "headers",
        "_encoded",
        "background",
        "_stream",
        "_ct_cache_key",
        "_mimetype",
        "_charset",
        "_ct_params",
    )

    #: Whether this response is a Server-Sent Events stream. `EventSourceResponse`
    #: overrides it to `True`. Declared here as a class attribute - legal
    #: alongside `__slots__` and costing nothing per instance - so the transport
    #: and compression paths can read it directly. They used
    #: `getattr(response, "is_event_source", False)`, which on a slotted class
    #: with no such attribute misses the slots *and* the MRO and pays CPython's
    #: full exception setup and teardown: ~38 ns on every response.
    is_event_source = False

    def __init__(
        self,
        status_code: int = HTTP_200_OK,
        body: bytes = b"",
        content_type: str = MIME_TEXT_PLAIN,
        headers: dict[str, str] | None = None,
        background: Any = None,
    ) -> None:
        self.status_code = status_code
        self._encoded: bytes | None = None
        self._body = body
        self.content_type = content_type
        # Copy on the way in, for the reason `HTTPException.__init__` already
        # records: this mapping becomes the response's `headers`, which response
        # middleware (CORS / Session / SecurityHeaders) and `set_cookie` mutate
        # in place. Aliasing the caller's dict lets one request's `Set-Cookie`
        # accumulate on a handler-held constant and ship on every later
        # response - a module-level `HEADERS = {"X-App-Version": "1.0"}` reused
        # across routes is the ordinary shape that leaks, and what leaks is a
        # signed session cookie belonging to a different user. The error path
        # was given this rule; the success path is the far more common one.
        # `None` stays free: no copy, just the fresh dict it always allocated.
        self.headers = dict(headers) if headers else {}
        # Optional `BackgroundTask` or `BackgroundTasks` fired by the
        # dispatch layer after this response is built. None when no task
        # is attached. `Response(content=..., background=BackgroundTask(fn))`.
        self.background = background
        # `StreamingResponse` rewrites this with an async iterator; for a
        # base `Response` the slot stays `None` so `is_streamed` is a
        # direct attribute load (no `getattr` fallback to None).
        self._stream: Any = None
        # Value-keyed parse cache for `mimetype` / `charset` /
        # `mimetype_params`. `content_type` is a bare public slot that
        # framework and user code reassign directly (no setter hook), so the
        # cache is keyed on the `content_type` *value* it was built from: a
        # mismatch on read means the value changed and the cache is rebuilt.
        # `_ct_cache_key` starts as `None`, which no real content-type string
        # equals, so the first access always parses.
        self._ct_cache_key: str | None = None
        self._mimetype: str = ""
        self._charset: str = ""
        self._ct_params: dict[str, str] = {}

    # ── `body` ────────────────────────────────────────────────
    # Backed by `_body` so the setter can invalidate the encode cache.
    # Middleware that mutates `response.body = new_bytes` after a prior
    # `.encode()` call would otherwise emit stale bytes + wrong
    # Content-Length on the next encode.

    @property
    def body(self) -> bytes:
        """Return the response body bytes."""
        return self._body

    @body.setter
    def body(self, value: bytes) -> None:
        """Set the response body, keeping any `Content-Length` in step.

        The refresh lives here, on the single assignment `body`, `data` and
        `set_data` all go through, so every spelling keeps the header in step:
        one that refreshed on `set_data` alone would let a middleware rewriting
        `response.body` - the obvious spelling - advertise the old length and
        desynchronise a keep-alive connection. The constructor assigns `_body`
        directly, so a response that never reassigns its body pays nothing.
        """
        self._body = value
        stored = header_key(self.headers, HEADER_CONTENT_LENGTH)
        if stored is not None:
            self.headers[stored] = str(len(value))
        self._encoded = None

    # ── `media_type` alias ────────────────────────────────────
    # ASGI servers name this attribute `media_type`; veloce's
    # canonical name is `content_type`. Expose both names so code that
    # uses either name reads and writes cleanly.

    @property
    def media_type(self) -> str:
        """Return the full Content-Type including parameters."""
        return self.content_type

    @media_type.setter
    def media_type(self, value: str) -> None:
        """Set the media type."""
        self.content_type = value
        # Invalidate any cached HTTP/1.1 encode so the new content type
        # takes effect on the next `encode()` call.
        self._encoded = None

    # ── `mimetype` ────────────────────────────────────────────
    # `mimetype` is the bare media type, with no parameters.
    # Setting it preserves the existing `charset` parameter.

    @property
    def is_json(self) -> bool:
        """True when `Content-Type` is JSON.

        Matches `application/json` and any `application/*+json`
        structured suffix (RFC 6839 Sec. 3.1).
        """
        return is_json_mimetype(self.mimetype)

    def get_json(self) -> Any:
        """Parse the response body as JSON.

        Returns `None` for an empty body. Useful in tests to inspect a
        JSON response without re-decoding `body` by hand. Raises if the
        body is non-empty and not valid JSON.
        """
        body = self.body
        return orjson.loads(body) if body else None

    def _ensure_ct_parsed(self) -> None:
        """Parse `content_type` into mimetype / charset / params, value-keyed.

        Re-parses only when the current `content_type` differs from the value
        the cache was built from, so direct slot reassignment can never serve a
        stale result. A warm cache costs one string compare.
        """
        ct = self.content_type or ""
        if ct == self._ct_cache_key:
            return
        media, semi, rest = ct.partition(";")
        self._mimetype = media.strip().lower()
        # `partition` already reports whether there were parameters at all.
        params = dict(parse_media_type_params(rest)) if semi else {}
        self._ct_params = params
        # RFC 9110 Sec. 8.3.1 makes media-type parameter names case-insensitive
        # and RFC 9110 Sec. 5.6.6 allows whitespace around `=`, so the charset
        # is read off the shared parser's output rather than re-scanned here -
        # one parse, and `charset` can never disagree with `mimetype_params`.
        self._charset = params.get("charset", "utf-8")
        self._ct_cache_key = ct

    @property
    def mimetype(self) -> str:
        """The bare media type - `Content-Type` without parameters.

        `text/html; charset=utf-8` -> `text/html`. Lower-cased and
        stripped per RFC 9110 Sec. 8.3 (media types are case-insensitive).
        """
        self._ensure_ct_parsed()
        return self._mimetype

    @mimetype.setter
    def mimetype(self, value: str) -> None:
        """Set the mimetype."""
        # Preserve the current charset parameter, if any. The presence test
        # reads the parsed parameter map rather than the raw header so a
        # `Charset=` spelling is carried over too (RFC 9110 Sec. 8.3.1).
        cs = self.charset
        had_charset = "charset" in self.mimetype_params
        self.content_type = f"{value}; charset={cs}" if had_charset else value
        self._encoded = None

    # ── `status` line ─────────────────────────────────────────
    # `response.status` is the full status line
    # ("200 OK"), with `status_code` as the bare int. veloce's
    # canonical field is `status_code`; `status` is the string view.

    @property
    def status(self) -> str:
        """Full HTTP status line, e.g. `"200 OK"`.

        Assignable: accepts an int (`200`), a bare numeric string
        (`"200"`), or a full status line (`"200 OK"` / `"404 Not
        Found"`). The leading integer is parsed into `status_code`.
        """
        phrase = _STATUS_PHRASES.get(self.status_code, "")
        return f"{self.status_code} {phrase}".rstrip()

    @status.setter
    def status(self, value: int | str) -> None:
        """Set the status code."""
        if isinstance(value, int):
            self.status_code = value
        else:
            # Take the leading integer token of "404 Not Found" / "404".
            stripped = value.strip()
            if not stripped:
                raise ValueError("Response.status: empty value")
            head = stripped.split(None, 1)[0]
            self.status_code = int(head)
        self._encoded = None

    # ── Wire encoding ─────────────────────────────────────────

    def encode(self, keep_alive: bool = True) -> bytes:
        """Encode to raw HTTP/1.1 bytes - called once, cached.

        `keep_alive` is what the transport decided about this connection; the
        head advertises it rather than always claiming `keep-alive`. Only the
        keep-alive encode is cached, so a response re-encoded for a closing
        connection cannot serve a stale `Connection` line, and the common path
        keeps its single cached blob.
        """
        if keep_alive and self._encoded is not None:
            return self._encoded

        # Bodiless statuses (1xx/204/205/304) carry no payload and no default
        # content-type - matching the ASGI emit path (single source of truth
        # via `status_permits_body`). A handler-set content-type in `self.headers`
        # still wins; only the framework default is suppressed. A 304 (like
        # HEAD) may advertise the would-be-200 Content-Length while sending no
        # body (RFC 9110 Sec. 8.6); 1xx/204/205 advertise 0.
        body_allowed = status_permits_body(self.status_code)
        is_304 = self.status_code == HTTP_304_NOT_MODIFIED
        body = self.body if body_allowed else b""
        advertised_length = len(self.body) if (body_allowed or is_304) else 0
        default_headers = {HEADER_CONTENT_LENGTH: str(advertised_length)}
        if body_allowed:
            default_headers = {HEADER_CONTENT_TYPE: self.content_type, **default_headers}
        parts = _encode_response_head(
            self.status_code, default_headers, self.headers, keep_alive=keep_alive
        )
        parts.append("\r\n")

        encoded = "".join(parts).encode("latin-1") + body
        if keep_alive:
            self._encoded = encoded
        return encoded

    # ── Cookies ───────────────────────────────────────────────

    def set_cookie(
        self,
        key: str,
        value: str,
        max_age: Any = None,
        expires: Any = None,
        path: str = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: str | None = "Lax",
        partitioned: bool = False,
        prefix: Literal["host", "secure"] | None = None,
    ) -> None:
        """Build a `Set-Cookie` header per RFC 6265.

        The cookie name must be a valid RFC 6265 token (no spaces, separators,
        or control characters) and must not collide with a cookie-attribute
        keyword (`Path`, `Max-Age`, ...); a violation raises `ValueError`.

        `samesite` defaults to `"Lax"` - a CSRF-resistant default that
        matches modern browser behaviour. Pass `samesite="None"` (with
        `secure=True`) for a cookie that must travel on cross-site
        requests, or `samesite=None`/`""` to omit the attribute.

        `expires=` accepts a `datetime`, a Unix timestamp `int|float`,
        or an already-formatted IMF-fixdate `str`. When both `max_age`
        and `expires` are set, both are emitted (RFC 6265 Sec. 5.2.2: clients
        prefer `Max-Age` when supported, falling back to `Expires` on
        legacy IE).

        `partitioned=True` adds the CHIPS `Partitioned` attribute
        (Cookies Having Independent Partitioned State) - a partitioned
        cookie is keyed to the top-level site, so embedded third-party
        contexts each get an isolated jar. `Partitioned` requires
        `Secure`, so it is only emitted when `secure=True`.

        `prefix="host"` / `prefix="secure"` add the RFC 6265bis Sec. 4.1.3
        name prefix (`__Host-` / `__Secure-`) and enforce its invariants:
        `"secure"` requires `secure=True`; `"host"` also requires `path="/"`
        and no `domain`. A violation raises `ValueError`.

        The cookie name and value are rejected if they contain CR, LF, or
        NUL - untrusted data must not be able to inject additional cookies
        or response headers. `dump_cookie` performs that CRLF check on all
        five fields (name, value, domain, path, samesite), so `set_cookie`
        does not repeat it.
        """
        # dump_cookie accepts datetime and numeric timestamps but not
        # pre-formatted strings. Handle the string case separately.
        expires_str: str | None = None
        dump_expires = expires
        if isinstance(expires, str):
            expires_str = expires
            dump_expires = None

        cookie = dump_cookie(
            key,
            value,
            max_age=max_age,
            expires=dump_expires,
            path=path,
            domain=domain,
            secure=secure,
            httponly=httponly,
            samesite=samesite,
            prefix=prefix,
        )
        if expires_str is not None:
            cookie += f"; Expires={expires_str}"
        # CHIPS `Partitioned` - only valid alongside `Secure`.
        if partitioned and secure:
            cookie += "; Partitioned"
        self._append_set_cookie_header(cookie)
        self._encoded = None

    def _append_set_cookie_header(self, raw_value: str) -> None:
        """Append a serialised `Set-Cookie` line, keeping the earlier ones.

        `raw_value` is one serialised line, or a `\\r\\nSet-Cookie: `-joined
        multi-cookie blob. The single canonical home for that join format.
        """
        existing = self.headers.get(HEADER_SET_COOKIE)
        if existing:
            self.headers[HEADER_SET_COOKIE] = existing + SET_COOKIE_JOINER + raw_value
        else:
            self.headers[HEADER_SET_COOKIE] = raw_value

    # ── Body and its measurements ─────────────────────────────

    @property
    def content_length(self) -> int:
        """Length of the response body in bytes.

        Always derived from `len(body)`. Streaming responses (which
        don't materialise the body) return 0 here; see `is_streamed`.
        """
        return len(self.body)

    @property
    def is_streamed(self) -> bool:
        """`True` when the response body is a streaming iterator."""
        return self._stream is not None

    @property
    def charset(self) -> str:
        """Response charset from `Content-Type`.

        Falls back to `"utf-8"` when no charset parameter is present.
        Assignable: setting it rewrites the `charset=` parameter on the
        existing `Content-Type` (the bare media type is preserved).
        """
        self._ensure_ct_parsed()
        return self._charset

    @charset.setter
    def charset(self, value: str) -> None:
        """Set the charset."""
        ct = self.content_type or MIME_TEXT_PLAIN
        # Keep the bare media type, drop any existing parameters, then
        # re-attach the new charset.
        media = ct.split(";", 1)[0].strip()
        self.content_type = f"{media}; charset={value}"
        self._encoded = None

    @property
    def mimetype_params(self) -> dict[str, str]:
        """Parameters of the `Content-Type` header.

        Everything after the bare media type, as a dict of lower-cased
        parameter names to their (unquoted) values. For
        `text/html; charset=utf-8` this is `{"charset": "utf-8"}`.
        Returns an empty dict when no parameters are present.
        """
        self._ensure_ct_parsed()
        return self._ct_params

    def calculate_content_length(self) -> int:
        """Set `Content-Length` from `len(body)` and return the value.

        Useful when a caller mutates `body` directly and wants the
        header to follow. The ASGI emit path computes Content-Length
        from `body` on the fly anyway; this helper is for callers that
        want it locked into `self.headers` ahead of time.
        """
        n = len(self.body)
        self.headers[HEADER_CONTENT_LENGTH] = str(n)
        self._encoded = None
        return n

    # ── Validators and dates ──────────────────────────────────

    @property
    def last_modified(self) -> datetime | None:
        """Parsed `Last-Modified` header -> UTC `datetime` or None.

        Accepts the three RFC 9110 Sec. 5.6.7 HTTP-date
        forms. Returns `None` on missing/unparseable.
        """
        raw = header_get(self.headers, HEADER_LAST_MODIFIED)
        if not raw:
            return None
        return parse_date(raw)

    @last_modified.setter
    def last_modified(self, value: Any) -> None:
        """Set the `Last-Modified` header."""
        self._set_http_date_header(HEADER_LAST_MODIFIED, value)

    @property
    def expires(self) -> datetime | None:
        """Parsed `Expires` header -> UTC `datetime` or None (RFC 9111 Sec. 5.3)."""
        raw = header_get(self.headers, HEADER_EXPIRES)
        if not raw:
            return None
        return parse_date(raw)

    @expires.setter
    def expires(self, value: Any) -> None:
        """Set the `Expires` header."""
        self._set_http_date_header(HEADER_EXPIRES, value)

    def _set_http_date_header(self, name: str, value: Any) -> None:
        """Set an HTTP-date header from datetime / unix ts / preformatted str.

        `value=None` removes the header under whatever casing it was stored.
        Popping only the canonical and lower-case spellings left a third one on
        the wire while the getter, which reads case-insensitively, reported the
        clear had worked. Naive datetimes are interpreted as UTC.
        """
        if value is None:
            stored = header_key(self.headers, name)
            if stored is not None:
                del self.headers[stored]
            self._encoded = None
            return
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if isinstance(value, (datetime, date, int, float)):
            self.headers[name] = http_date(value)
        else:
            self.headers[name] = str(value)
        self._encoded = None

    @property
    def cookies(self) -> dict[str, str]:
        """Parsed cookie jar from this response's `Set-Cookie` header(s).

        Walks every `Set-Cookie` entry (separator `\\r\\nSet-Cookie: `
        respected) and returns `{name: value}`. Values are percent-decoded,
        so a cookie reads back as the string `set_cookie()` was given rather
        than its wire form. Multiple cookies with the same name resolve to
        the last set - matches the wire behaviour where the client also keeps
        the most-recent value. Caller introspection only; mutation goes
        through `set_cookie()`.
        """
        out: dict[str, str] = {}
        existing = header_get(self.headers, HEADER_SET_COOKIE) or ""
        if not existing:
            return out
        # Multi-cookies are emitted as `cookie1\r\nSet-Cookie: cookie2...`. Only
        # the leading `name=value` segment of each entry is the cookie; the
        # rest are attributes. `iter_cookies` is the inverse of `dump_cookie`'s
        # percent-quoting, and the same parser `Request.cookies` reads with.
        for line in existing.split(SET_COOKIE_JOINER):
            out.update(iter_cookies(line.split(";", 1)[0]))
        return out

    @property
    def headerlist(self) -> list[tuple[str, str]]:
        """Headers flattened to the `(name, value)` tuple list the wire emit sends.

        Each `Set-Cookie` (multi-cookie join) expands to its own tuple, so
        the caller gets the per-cookie view ASGI requires. Two spellings of one
        field name collapse to a single entry carrying the last name and value
        seen, at the position of the first (RFC 9110 Sec. 5.1 makes field names
        case-insensitive), and a CR/LF/NUL anywhere in a name or value raises
        `ValueError` rather than being handed back - both matching the emit
        paths. `Response.headers` remains the raw, unfolded view.
        """
        result: list[tuple[str, str]] = []
        slot_by_name: dict[str, int] = {}
        for k, v in self.headers.items():
            k_lower = k.lower()
            if k_lower == "set-cookie":
                for piece in v.split(SET_COOKIE_JOINER):
                    cookie = piece.strip()
                    _reject_header_crlf(cookie, MSG_LABEL_SET_COOKIE_VALUE)
                    result.append((k, cookie))
            else:
                # The per-header failure label is an f-string, so build it only
                # on the path that actually raises.
                if _header_value_has_crlf(k) or _header_value_has_crlf(v):
                    _reject_header_crlf(k, MSG_LABEL_HEADER_NAME)
                    _reject_header_crlf(v, f"{k} header value")
                slot = slot_by_name.get(k_lower)
                if slot is None:
                    slot_by_name[k_lower] = len(result)
                    result.append((k, v))
                else:
                    result[slot] = (k, v)
        return result

    @property
    def data(self) -> bytes:
        """Body bytes alias for `Response.body`.

        Read returns the current body; writing through the setter
        replaces the body, invalidates any cached HTTP/1.1 encoded bytes
        (`_encoded`), and updates `Content-Length` when the headers carry one.
        """
        return self.body

    @data.setter
    def data(self, value: bytes | str) -> None:
        """Set the response body; alias for `set_data`."""
        self.set_data(value)

    def set_data(self, value: bytes | str) -> None:
        """Replace the response body.

        Accepts `bytes` or `str` (UTF-8 encoded). Invalidates the cached
        HTTP/1.1 encoding so the new body goes on the wire at the next emit,
        and refreshes `Content-Length` when the headers carry one. Assigning
        `response.body` directly does the same.
        """
        if isinstance(value, str):
            value = value.encode("utf-8")
        # The `body` property setter clears `_encoded` and refreshes any
        # `Content-Length`; no separate invalidation or refresh needed.
        self.body = value

    # ── Caching and negotiation ───────────────────────────────

    def set_cache_control(
        self,
        max_age: int | None = None,
        public: bool = False,
        private: bool = False,
        no_cache: bool = False,
        no_store: bool = False,
        must_revalidate: bool = False,
        immutable: bool = False,
        s_maxage: int | None = None,
    ) -> str:
        """Build and set the `Cache-Control` header - RFC 9111 Sec. 5.2.

        Combines the standard directives in the order RFC 9111 Sec. 5.2
        documents. Values that are False / None are omitted, so a plain
        `resp.set_cache_control(max_age=3600, public=True)` produces
        `Cache-Control: public, max-age=3600`. Returns the value set.
        """
        parts: list[str] = []
        if public:
            parts.append(HEADER_VALUE_PUBLIC)
        if private:
            parts.append("private")
        if no_cache:
            parts.append(HEADER_VALUE_NO_CACHE)
        if no_store:
            parts.append("no-store")
        if must_revalidate:
            parts.append("must-revalidate")
        if immutable:
            parts.append("immutable")
        if max_age is not None:
            parts.append(f"max-age={max_age}")
        if s_maxage is not None:
            parts.append(f"s-maxage={s_maxage}")
        value = ", ".join(parts)
        if value:
            self.headers[HEADER_CACHE_CONTROL] = value
            self._encoded = None
        return value

    def add_vary(self, *header_names: str) -> str:
        """Append header names to the `Vary` response header - RFC 9110 Sec. 12.5.5.

        Merges with any existing `Vary` value (de-duplicates,
        case-insensitive). Returns the resulting header value.
        Useful when middleware wants to communicate "this response
        depends on the named request headers" without clobbering
        existing entries.
        """
        headers = self.headers
        # One scan serves both paths: it decides whether a `Vary` already exists
        # (under any casing) and, if so, names the key to read and clear.
        #
        # Every casing, not just `Vary` and `vary`. It is tempting to argue
        # that a third casing merely produces two `Vary` field lines, which RFC
        # 9110 Sec. 5.2 says a recipient combines - but both emit paths fold
        # duplicate field names and keep the last write
        # (`_build_asgi_headers`, `_encode_response_head`), so only one line is
        # ever sent and the earlier value is dropped, not combined. Missing a
        # casing puts `VARY: Cookie` on the wire as `Vary: Accept-Encoding`
        # alone - and a `Vary: Cookie` a shared cache never sees is how one
        # user's response gets served to another. The scan is short-circuited on
        # the canonical spelling inside `header_key`, so the ordinary case is one
        # dict lookup.
        stored_key = header_key(headers, HEADER_VARY)
        # Fast path - no existing `Vary` and a single clean token (a middleware
        # adding `Vary: Cookie` per response) needs no parse/merge: the token
        # round-trips through `HeaderSet` unchanged, so set it directly and skip
        # the list+set allocation. This runs on every session-touched response.
        if len(header_names) == 1 and stored_key is None:
            value = header_names[0]
            headers[HEADER_VARY] = value
            self._encoded = None
            return value
        existing = headers[stored_key] if stored_key is not None else ""
        # The merge is a pure function of what is already there and what is
        # being added, and both are fixed per application: middleware order does
        # not change between requests, so a stack contributing `Origin`, then
        # `Accept-Encoding`, then `Cookie` asks the same two questions on every
        # response. Measured at 5.3 us for the second and third calls against
        # 0.5 us for the first, which takes the fast path above.
        cache_key = (existing, header_names)
        cached = _VARY_MERGES.get(cache_key)
        if cached is not None:
            value = cached
        else:
            # Delegate dedup + ordering to `HeaderSet` so the same
            # case-insensitive merge logic doesn't drift between this method
            # and the `vary` property's own datastructure.
            merged = HeaderSet(existing)
            merged.update(header_names)
            value = merged.to_header()
            # Bounded: a handler may write `response.headers["Vary"]` from user
            # input, so `existing` is not always framework-controlled. Cleared
            # outright rather than evicted one entry, matching
            # `_ENCODED_HEADER_PAIRS` on the emit path.
            if len(_VARY_MERGES) >= _MAX_VARY_MERGES:
                _VARY_MERGES.clear()
            _VARY_MERGES[cache_key] = value
        # Always write under `Vary` (canonical case), clearing whatever casing
        # the value was actually stored under.
        if stored_key is not None:
            del headers[stored_key]
        headers[HEADER_VARY] = value
        self._encoded = None
        return value

    @property
    def vary(self) -> HeaderSet:
        """The `Vary` header as a `HeaderSet`.

        Returns a fresh `HeaderSet` parsed from the current header.
        Assign a `HeaderSet`, iterable of strings, or a comma-separated
        string to replace it. Mutating the returned object does *not*
        write back - call `add_vary(...)` or reassign for that.
        """
        return HeaderSet(header_get(self.headers, HEADER_VARY) or "")

    @vary.setter
    def vary(self, value: Any) -> None:
        """Set the `Vary` header."""
        self._set_header_set(HEADER_VARY, value)

    @property
    def allow(self) -> HeaderSet:
        """The `Allow` header as a `HeaderSet`.

        Lists the HTTP methods the resource supports (RFC 9110 Sec. 10.2.1).
        Assign a `HeaderSet`, iterable, or comma-separated string.
        """
        return HeaderSet(header_get(self.headers, HEADER_ALLOW) or "")

    @allow.setter
    def allow(self, value: Any) -> None:
        """Set the `Allow` header."""
        self._set_header_set(HEADER_ALLOW, value)

    # ── Authentication challenge ──────────────────────────────

    @property
    def www_authenticate(self) -> str | None:
        """The `WWW-Authenticate` challenge header - RFC 9110 Sec. 11.6.1.

        Sent on `401 Unauthorized` to tell the client which auth
        scheme(s) to use. `None` when unset.
        """
        return header_get(self.headers, HEADER_WWW_AUTHENTICATE)

    @www_authenticate.setter
    def www_authenticate(self, value: str | None) -> None:
        """Set the `WWW-Authenticate` header."""
        if value is None:
            header_pop(self.headers, HEADER_WWW_AUTHENTICATE)
        else:
            # Caller-supplied challenges may interpolate a realm or
            # token68; reject CRLF here so this low-level setter has the
            # same header-injection guarantees as set_basic_auth_challenge.
            _reject_header_crlf(value, HEADER_WWW_AUTHENTICATE)
            self.headers[HEADER_WWW_AUTHENTICATE] = value
        self._encoded = None

    def set_basic_auth_challenge(self, realm: str = "Authentication Required") -> str:
        """Write a `Basic` `WWW-Authenticate` challenge - RFC 7617.

        Convenience for the common 401 case:
        `WWW-Authenticate: Basic realm="<realm>", charset="UTF-8"`.
        Returns the header value written.
        """
        _reject_header_crlf(realm, "realm")
        value = f'{AUTH_SCHEME_BASIC} realm="{realm}", charset="UTF-8"'
        self.headers[HEADER_WWW_AUTHENTICATE] = value
        self._encoded = None
        return value

    def _set_header_set(self, name: str, value: Any) -> None:
        """Replace header `name` with the `HeaderSet` form of `value`.

        Backs the list-valued headers whose setter accepts a `HeaderSet`,
        an iterable of strings, or a comma-separated string; invalidates
        the cached encode after every mutation.
        """
        hs = value if isinstance(value, HeaderSet) else HeaderSet(value)
        header_pop(self.headers, name)
        self.headers[name] = hs.to_header()
        self._encoded = None

    def _set_or_pop(self, name: str, value: str | None) -> None:
        """Set header `name` to `value`, or remove it when `value is None`.

        Backs the plain string headers whose setter is a pop/assign pair;
        invalidates the cached encode after every mutation.
        """
        if value is None:
            header_pop(self.headers, name)
        else:
            self.headers[name] = value
        self._encoded = None

    # ── Single-value header accessors ─────────────────────────

    @property
    def content_encoding(self) -> str | None:
        """The `Content-Encoding` header - RFC 9110 Sec. 8.4. `None` when unset."""
        return header_get(self.headers, HEADER_CONTENT_ENCODING)

    @content_encoding.setter
    def content_encoding(self, value: str | None) -> None:
        """Set the `Content-Encoding` header."""
        self._set_or_pop(HEADER_CONTENT_ENCODING, value)

    @property
    def content_language(self) -> str | None:
        """The `Content-Language` header - RFC 9110 Sec. 8.5. `None` when unset."""
        return header_get(self.headers, HEADER_CONTENT_LANGUAGE)

    @content_language.setter
    def content_language(self, value: str | None) -> None:
        """Set the `Content-Language` header."""
        self._set_or_pop(HEADER_CONTENT_LANGUAGE, value)

    @property
    def accept_ranges(self) -> str | None:
        """The `Accept-Ranges` header - RFC 9110 Sec. 14.3.

        Typically `bytes` (range requests supported) or `none`
        (explicitly unsupported). `None` when the header is unset.
        """
        return header_get(self.headers, HEADER_ACCEPT_RANGES)

    @accept_ranges.setter
    def accept_ranges(self, value: str | None) -> None:
        """Set the `Accept-Ranges` header."""
        self._set_or_pop(HEADER_ACCEPT_RANGES, value)

    def set_content_range(
        self,
        start: int | None,
        stop: int | None,
        length: int | None,
        unit: str = HEADER_VALUE_BYTES,
    ) -> str:
        """Write a `Content-Range` header - RFC 9110 Sec. 14.4.

        - `set_content_range(0, 499, 1234)` -> `bytes 0-499/1234`.
        - `start`/`stop` both `None` -> an unsatisfied-range response:
          `bytes */1234` (length required in that form).
        - `length` `None` -> unknown total: `bytes 0-499/*`.

        Returns the header value written.
        """
        if start is None or stop is None:
            total = "*" if length is None else str(length)
            value = f"{unit} */{total}"
        else:
            total = "*" if length is None else str(length)
            value = f"{unit} {start}-{stop}/{total}"
        self.headers[HEADER_CONTENT_RANGE] = value
        self._encoded = None
        return value

    @property
    def content_range(self) -> str | None:
        """The raw `Content-Range` header - RFC 9110 Sec. 14.4. `None` if unset."""
        return header_get(self.headers, HEADER_CONTENT_RANGE)

    @property
    def date(self) -> datetime | None:
        """The `Date` header as a tz-aware UTC `datetime` - RFC 9110 Sec. 6.6.1.

        Returns `None` when unset or unparseable. Assign a `datetime`
        or POSIX timestamp to set it; assign `None` to remove it.
        """
        return parse_date(header_get(self.headers, HEADER_DATE))

    @date.setter
    def date(self, value: Any) -> None:
        """Set the `Date` header."""
        if value is None:
            header_pop(self.headers, HEADER_DATE)
        else:
            self.headers[HEADER_DATE] = http_date(value)
        self._encoded = None

    @property
    def location(self) -> str | None:
        """The `Location` header - RFC 9110 Sec. 10.2.2. `None` when unset."""
        return header_get(self.headers, HEADER_LOCATION)

    @location.setter
    def location(self, value: str | None) -> None:
        """Set the `Location` header."""
        self._set_or_pop(HEADER_LOCATION, value)

    @property
    def content_location(self) -> str | None:
        """The `Content-Location` header - RFC 9110 Sec. 8.7. `None` when unset."""
        return header_get(self.headers, HEADER_CONTENT_LOCATION)

    @content_location.setter
    def content_location(self, value: str | None) -> None:
        """Set the `Content-Location` header, or remove it with `None`."""
        self._set_or_pop(HEADER_CONTENT_LOCATION, value)

    @property
    def retry_after(self) -> int | datetime | None:
        """The `Retry-After` header - RFC 9110 Sec. 10.2.3.

        Returns an `int` (delay in seconds) when the header is numeric,
        a tz-aware `datetime` when it's an HTTP-date, or `None` when
        unset. Assign an int / `timedelta` / `datetime` to set it;
        assign `None` to remove it.
        """
        raw = header_get(self.headers, HEADER_RETRY_AFTER)
        if not raw:
            return None
        raw = raw.strip()
        if raw.isdigit():
            return int(raw)

        return parse_date(raw)

    @retry_after.setter
    def retry_after(self, value: Any) -> None:
        """Set the `Retry-After` header."""
        if value is None:
            header_pop(self.headers, HEADER_RETRY_AFTER)
        elif isinstance(value, timedelta):
            self.headers[HEADER_RETRY_AFTER] = str(int(value.total_seconds()))
        elif isinstance(value, datetime):
            self.headers[HEADER_RETRY_AFTER] = http_date(value)
        else:
            self.headers[HEADER_RETRY_AFTER] = str(int(value))
        self._encoded = None

    @property
    def age(self) -> int | None:
        """The `Age` header in seconds - RFC 9110 Sec. 5.1. `None` when unset."""
        raw = header_get(self.headers, HEADER_AGE)
        if not raw or not raw.strip().isdigit():
            return None
        return int(raw.strip())

    @age.setter
    def age(self, value: int | None) -> None:
        """Set the `Age` header in seconds, or remove it with `None`."""
        if value is None:
            header_pop(self.headers, HEADER_AGE)
        else:
            self.headers[HEADER_AGE] = str(int(value))
        self._encoded = None

    # ── ETags ─────────────────────────────────────────────────

    def set_etag(self, etag: str, weak: bool = False) -> None:
        """Set the `ETag` header from an explicit value.

        Quotes the value if the caller passed it bare. Prepends `W/`
        when `weak=True`. Use `add_etag()` for body-derived MD5
        ETags; `set_etag` is for callers that already have an
        authoritative tag (DB revision, commit hash, version
        counter).
        """
        if not etag.startswith('"'):
            etag = f'"{etag}"'
        if weak and not etag.startswith("W/"):
            etag = "W/" + etag
        self.headers[HEADER_ETAG] = etag
        self._encoded = None

    def get_etag(self) -> tuple[str | None, bool]:
        """Return `(etag, is_weak)` parsed from the `ETag` header.

        `(None, False)` when unset. Returned tag keeps its quotes so
        it compares directly with `If-None-Match` values.
        """
        raw = header_get(self.headers, HEADER_ETAG)
        if not raw:
            return (None, False)
        if raw.startswith("W/"):
            return (raw[2:], True)
        return (raw, False)

    def freeze(self) -> None:
        """Pre-compute the cached HTTP/1.1 encode.

        For buffered responses, populates `_encoded` so subsequent
        access pays no encode cost. For streaming responses, no-op.
        Used by response caching layers that want immutable bytes.
        """
        if self._stream is not None:
            return
        if self._encoded is None:
            self.encode()

    @property
    def cache_control(self) -> CacheControl:
        """Parsed `Cache-Control` header (read-only view).

        For setting directives, prefer `set_cache_control(...)` which
        writes the header directly. This property is convenient for
        introspection: `resp.cache_control.max_age`,
        `resp.cache_control.no_store`, etc.
        """
        return CacheControl(header_get(self.headers, HEADER_CACHE_CONTROL) or "")

    # ── Iteration ─────────────────────────────────────────────

    def iter_encoded(self) -> Any:
        """Yield the response body.

        Return type is mode-dependent and the two modes are NOT
        interchangeable:

        - Buffered response (`is_streamed is False`) -> returns a
          synchronous iterator yielding `bytes`. Drain with `for`.
        - Streaming response (`is_streamed is True`) -> returns the
          underlying async iterator (`AsyncIterator[bytes]`). Drain
          with `async for`.

        Callers must branch on `response.is_streamed` (or use
        `inspect.isasyncgen` / `hasattr(it, "__aiter__")`) to pick
        the right loop, e.g.:

            it = response.iter_encoded()
            if response.is_streamed:
                async for chunk in it:
                    ...
            else:
                for chunk in it:
                    ...

        """
        stream = self._stream
        if stream is not None:
            return stream
        return iter([self.body]) if self.body else iter([])

    def iter_chunked(self, size: int) -> Any:
        """Yield the response body in fixed-size chunks.

        Return type is mode-dependent and the two modes are NOT
        interchangeable:

        - Buffered response (`is_streamed is False`) -> returns a
          synchronous generator yielding `bytes` slices of length
          `size` (the final slice may be shorter). Drain with `for`.
        - Streaming response (`is_streamed is True`) -> returns the
          underlying async iterator unchanged (`AsyncIterator[bytes]`);
          `size` is ignored because chunk boundaries are controlled by
          the source generator, not the caller. Drain with `async for`.

        Pick the loop based on `response.is_streamed`:

            it = response.iter_chunked(4096)
            if response.is_streamed:
                async for chunk in it:
                    ...
            else:
                for chunk in it:
                    ...

        `size` must be positive.
        """
        if size <= 0:
            raise ValueError("iter_chunked size must be positive")
        stream = self._stream
        if stream is not None:
            return stream
        body = self.body
        return (body[i : i + size] for i in range(0, len(body), size))

    if TYPE_CHECKING:  # pragma: no cover
        # The counterpart to `is_streamed`, which the base already answers.
        # `StreamingResponse`, `FileResponse` and `EventSourceResponse` define
        # it; the built-in server reaches it only behind that guard, and cannot
        # name those types because `serving/` must not import `sse`. Declared
        # for the type only - no runtime method, so this is not an abstract base
        # and a buffered response still has no `stream_to` to call by mistake.
        async def stream_to(
            self,
            transport: Any,
            drain: Callable[[], Awaitable[None]] | None = None,
            keep_alive: bool = True,
        ) -> None: ...

    def _encode_streaming_head(self, default_headers: dict[str, str], keep_alive: bool) -> bytes:
        """Encode a streaming response's head, honouring the bodiless-status rule.

        A bodiless status carries neither a payload nor framing for one: RFC
        9112 Sec. 6.1 forbids `Transfer-Encoding` on a 204, and a 204 that ships
        chunks desynchronises a keep-alive connection because the client reads
        them as the next response. `default_headers` (the content type plus the
        chunked framing) is therefore dropped for `Content-Length: 0` on such a
        status. Every streaming subclass encodes its head through here so the
        rule cannot hold on one of them and not another.
        """
        if not status_permits_body(self.status_code):
            default_headers = {HEADER_CONTENT_LENGTH: "0"}
        parts = _encode_response_head(
            self.status_code, default_headers, self.headers, keep_alive=keep_alive
        )
        parts.append("\r\n")
        return "".join(parts).encode("latin-1")

    def add_etag(self, weak: bool = False) -> str:
        """Compute and attach an ETag derived from the body.

        Uses MD5 of the response body, opaque-quoted per RFC 9110 Sec. 8.8.3.
        `weak=True` prepends `W/` so the validator is treated as a
        weak match (matching content but possibly different
        byte-for-byte). Sets `ETag` even if one was already set; pass
        the explicit ETag in `__init__(headers=...)` to skip this.
        Returns the value set.
        """
        # `usedforsecurity=False` (CPython 3.9+): this MD5 is an opaque cache
        # validator, not a security primitive, so it must not raise on FIPS
        # builds where MD5 is otherwise disabled. The digest bytes are unchanged.
        digest = hashlib.md5(self.body, usedforsecurity=False).hexdigest()
        etag = f'"{digest}"' if not weak else f'W/"{digest}"'
        self.headers[HEADER_ETAG] = etag
        self._encoded = None
        return etag

    # ── Conditional requests ──────────────────────────────────

    def make_conditional(self, request: Any) -> Response:
        """Downgrade this response to 304 on a precondition match.

        The request's preconditions are compared against the response's ETag
        and Last-Modified. Checks `If-None-Match` first (per RFC 9110
        Sec. 13.2 precedence),
        then `If-Modified-Since`. On a match, mutates `self` to status
        304 with no body. Returns `self` so callers can use it inline:
        `return resp.make_conditional(request)`.

        Handles `If-None-Match: *` (matches any current representation
        of the resource) and the weak/strong ETag comparison rules.
        """
        # If-None-Match: any token (or `*`) that equals the response's
        # ETag returns 304. Field names are case-insensitive (RFC 9110
        # Sec. 5.1), so a handler-set `Etag`/`etag` is honored too.
        ours_etag = header_get(self.headers, HEADER_ETAG) or ""
        inm = getattr(request, "if_none_match", ())
        if inm:
            # An `If-None-Match` the response cannot answer (no ETag of its own)
            # is not a match, and per Sec. 13.2 it still supersedes the date.
            if ours_etag and _preconditions_say_unchanged(inm, None, ours_etag, None):
                self._downgrade_to_304()
            return self

        # If-Modified-Since, only consulted when If-None-Match is absent.
        ours_lm = header_get(self.headers, HEADER_LAST_MODIFIED) or ""
        ims = getattr(request, "if_modified_since", None)
        if ims is not None and ours_lm:
            ours_dt = parse_date(ours_lm)
            if ours_dt is None:
                return self
            if _preconditions_say_unchanged((), ims, ours_etag, ours_dt.timestamp()):
                self._downgrade_to_304()
        return self

    def check_preconditions(self, request: Any) -> Response:
        """Enforce the write-side `If-Match` precondition (RFC 9110 Sec. 13.1.1).

        Raises `PreconditionFailed` (412) when the request carries an
        `If-Match` header that the response's current ETag does not satisfy
        under the strong comparison (Sec. 8.8.3.1) - the lost-update guard.
        `If-Match: *` is satisfied whenever a current representation exists,
        approximated here by the presence of an ETag header. With no
        `If-Match` header the response is returned unchanged. Returns `self`
        so it can be chained: `return resp.check_preconditions(request)`.

        Invoke this inside a handler (where `HTTPException` is converted to a
        response); it raises rather than mutating the status.
        """
        if_match = getattr(request, "if_match", ())
        if not if_match:
            # RFC 9110 Sec. 13.2.2: `If-Unmodified-Since` is evaluated only when
            # `If-Match` is absent. Skipping it left this the lost-update guard
            # in name only for a client that sent a date rather than an ETag -
            # `StaticFiles` has always honoured both, so the two doors disagreed
            # about what a precondition is.
            return self._check_unmodified_since(request)
        # `If-Match: *` is an existence precondition (RFC 9110 Sec. 13.1.1):
        # the handler producing this response means the resource exists, so it
        # is satisfied regardless of whether an ETag was attached.
        if if_match == ("*",):
            return self
        # `headers` is a plain dict; accept either spelling, as other helpers do.
        ours_etag = header_get(self.headers, HEADER_ETAG) or ""
        for tag in if_match:
            if _etag_matches_strong(ours_etag, tag):
                return self
        from veloce.exceptions import PreconditionFailed  # avoids response <-> exceptions cycle

        raise PreconditionFailed

    def _check_unmodified_since(self, request: Any) -> Response:
        """Enforce `If-Unmodified-Since` (RFC 9110 Sec. 13.1.4), or return self.

        Compared at HTTP-date resolution: the header carries whole seconds, so a
        `Last-Modified` with a fractional part must be floored or every
        comparison of a resource modified within the same second fails.
        """
        since = getattr(request, "if_unmodified_since", None)
        if since is None:
            return self
        raw = header_get(self.headers, HEADER_LAST_MODIFIED)
        if not raw:
            # No modification date to compare against, so the precondition
            # cannot be evaluated and is not a reason to refuse.
            return self
        parsed = parse_date(raw)
        # `parse_date` yields a datetime; `request.if_unmodified_since` yields a
        # Unix timestamp. Compare as whole seconds - an HTTP-date carries no
        # finer resolution, so anything else fails within the same second.
        if parsed is None or int(parsed.timestamp()) <= int(since):
            return self
        from veloce.exceptions import PreconditionFailed  # response <-> exceptions cycle

        raise PreconditionFailed

    def _downgrade_to_304(self) -> None:
        """Strip body + flip status to 304. Used by `make_conditional`."""
        # RFC 9110 Sec. 8.6: a 304 may carry the Content-Length a 200 would
        # have carried, which is what the emit paths advertise for a 304 built
        # directly. Record it before dropping the body - otherwise the length
        # is computed from the emptied body and the 304 claims the
        # representation is zero bytes, which RFC 9111 Sec. 4.3.4 then has
        # caches write over their stored length for a resource that is not
        # empty.
        # Idempotent: a handler may call `make_conditional` and a
        # conditional-GET middleware call it again on the way out. The second
        # pass sees an emptied body, so recomputing would replace the recorded
        # length with zero.
        if self.status_code == HTTP_304_NOT_MODIFIED:
            return
        representation_length = str(len(self.body))
        self.status_code = HTTP_304_NOT_MODIFIED
        self.body = b""
        header_pop(self.headers, HEADER_CONTENT_LENGTH)
        self.headers[HEADER_CONTENT_LENGTH] = representation_length
        self._encoded = None

    # ── Content-Disposition and cookie removal ────────────────

    def set_content_disposition(
        self, disposition: str = HEADER_VALUE_ATTACHMENT, filename: str | None = None
    ) -> str:
        """Write a `Content-Disposition` header - RFC 6266.

        `disposition` is `"attachment"` (force download) or `"inline"`
        (render in-browser). When `filename` is given, an ASCII quotable
        name uses `filename="..."` (spaces and punctuation preserved, only
        `\\` and `"` escaped); a non-ASCII or non-quotable name uses only
        the RFC 5987 `filename*=UTF-8''...` form, with no lossy legacy slot.
        Returns the header value written.
        """
        value = _format_content_disposition(disposition, filename) if filename else disposition
        self.headers[HEADER_CONTENT_DISPOSITION] = value
        self._encoded = None
        return value

    def delete_cookie(
        self,
        key: str,
        path: str = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: str | None = None,
        partitioned: bool = False,
        prefix: Literal["host", "secure"] | None = None,
    ) -> None:
        """Delete a cookie by overwriting it with an empty value + Max-Age=0.

        The browser only treats the new cookie as a replacement for the
        existing one if `Path`, `Domain`, **and the `Secure` / `SameSite`
        / `Partitioned` attributes match** - otherwise it stores both. So a
        session cookie originally set with `Secure; SameSite=None` (or with
        `Partitioned`) will not be deleted by a plain `delete_cookie(key)`
        call. Pass the same flags here. `prefix` deletes the cookie under
        its true `__Host-`/`__Secure-` wire name and enforces the same
        invariants on the deletion's attributes.
        """
        self.set_cookie(
            key,
            "",
            max_age=0,
            path=path,
            domain=domain,
            secure=secure,
            httponly=httponly,
            samesite=samesite,
            partitioned=partitioned,
            prefix=prefix,
        )


class JSONResponse(Response):
    """JSON response using orjson for speed.

    Usage::

        from veloce import JSONResponse

        async def handler(request):
            return JSONResponse({"ok": True}, status_code=200)
    """

    __slots__ = ()

    # Class-level default Content-Type. Subclasses (`ORJSONResponse`,
    # user-defined `class ProblemJSON(JSONResponse)`) override this to
    # change the content type emitted by both `__init__` and
    # `from_bytes` without re-implementing either. Named distinctly
    # from `Response.media_type` (which is an instance property aliasing
    # `content_type`) to avoid shadowing that property.
    default_media_type: str = MIME_JSON

    def __init__(
        self,
        data: Any,
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
        background: Any = None,
    ) -> None:
        try:
            # Through the app's provider, not a bare `orjson.dumps`: a
            # `JSON_SORT_KEYS` or a custom `json_provider_class` reached a
            # handler returning a dict and silently missed one returning this
            # class, so one application emitted two dialects. Outside a request
            # there is no app to ask and the direct encoder applies, which is
            # what `dumps_current` already resolves.
            body = dumps_current(data)
        except TypeError as exc:
            raise ValueError(f"JSONResponse data is not JSON-serializable: {exc}") from exc
        super().__init__(
            status_code=status_code,
            body=body,
            content_type=type(self).default_media_type,
            headers=headers,
            background=background,
        )

    @classmethod
    def _from_encoded(cls, body: bytes) -> JSONResponse:
        """Wrap already-encoded JSON bytes, for a caller that resolved the dialect.

        `from_bytes` is the public form and validates its argument; the dispatch
        path has just produced these bytes itself and has already asked the app
        which encoder to use, so it needs neither the check nor a second lookup
        through `__init__`.
        """
        resp = cls.__new__(cls)
        Response.__init__(
            resp,
            status_code=HTTP_200_OK,
            body=body,
            content_type=cls.default_media_type,
        )
        return resp

    @classmethod
    def from_bytes(
        cls,
        body: bytes,
        *,
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        """Build a `JSONResponse` from already-encoded JSON bytes.

        Skips `__init__`'s orjson re-encode - use this when the caller
        has produced the JSON body itself (e.g. with custom orjson
        options or via a `JSONProvider.dumps`). The body is sent
        verbatim with `Content-Type` taken from `cls.default_media_type`
        (so a subclass like `class ProblemJSON(JSONResponse):
        default_media_type = "application/problem+json"` gets its
        declared type without overriding this method).

        The caller is responsible for ensuring `body` is valid UTF-8
        JSON; no parsing or validation is performed. Passing non-JSON
        bytes will produce a response whose body does not match its
        declared content type.

        `body` must be `bytes` or `bytearray`. A `str` raises
        `TypeError` rather than being silently encoded, so callers do
        not produce a response with a mismatched charset by accident.

        Header precedence: when `headers` includes a `Content-Type`
        entry, the caller-supplied value wins and the class default is
        not emitted. This matches `Response`'s general rule that user
        headers override framework defaults and lets callers send
        `application/problem+json` or another JSON suffix type without
        subclassing.
        """
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError(
                "JSONResponse.from_bytes() requires bytes or bytearray, "
                f"got {type(body).__name__}. Encode the value first "
                "(e.g. body.encode('utf-8')) or use JSONResponse(data)."
            )
        resp = cls.__new__(cls)
        Response.__init__(
            resp,
            status_code=status_code,
            body=body if isinstance(body, bytes) else bytes(body),
            content_type=cls.default_media_type,
            headers=headers,
        )
        return resp


class ORJSONResponse(JSONResponse):
    """Explicit orjson-backed JSON response.

    `JSONResponse` already uses `orjson` for encoding, so this class is a
    semantic alias - useful when route declarations want to communicate
    the encoder choice via `response_class=ORJSONResponse`.
    """

    __slots__ = ()


class UJSONResponse(Response):
    """JSON response encoded with `ujson`.

    Lazily imports `ujson` at construction. Raises `ImportError` with a
    clear message when the package is missing rather than at module load,
    so apps that don't use this class don't need ujson installed.
    """

    __slots__ = ()

    def __init__(
        self,
        data: Any,
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        try:
            import ujson  # type: ignore[import-untyped]
        except ImportError as err:
            raise ImportError(
                "UJSONResponse requires the `ujson` package. Install it with: pip install ujson"
            ) from err
        body = ujson.dumps(data).encode("utf-8")
        super().__init__(
            status_code=status_code,
            body=body,
            content_type=MIME_JSON,
            headers=headers,
        )


class _TextResponse(Response):
    """Shared base for textual responses keyed only by content type.

    `content` is sent verbatim when already `bytes`, else UTF-8 encoded.
    Subclasses set only `default_media_type`, mirroring the
    `JSONResponse.default_media_type` idiom in this module.
    """

    __slots__ = ()

    default_media_type: str = MIME_PLAIN

    def __init__(
        self,
        content: str | bytes,
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
        background: Any = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            body=content if isinstance(content, bytes) else content.encode("utf-8"),
            content_type=type(self).default_media_type,
            headers=headers,
            background=background,
        )


class HTMLResponse(_TextResponse):
    """HTML response."""

    __slots__ = ()

    default_media_type = MIME_HTML


class PlainTextResponse(_TextResponse):
    """Plain text response."""

    __slots__ = ()

    default_media_type = MIME_PLAIN


class RedirectResponse(Response):
    """HTTP redirect."""

    __slots__ = ()

    def __init__(
        self,
        url: str,
        status_code: int = HTTP_307_TEMPORARY_REDIRECT,
        headers: dict[str, str] | None = None,
    ) -> None:
        hdrs = headers or {}
        # Reject CR/LF in the target and percent-encode it, so a crafted
        # URL or Host header cannot inject extra response headers. The
        # safe set keeps URL-structural characters (RFC 3986) and `%`
        # so an already-encoded URL is not double-encoded.
        _reject_header_crlf(url, "redirect URL")
        hdrs[HEADER_LOCATION] = quote(url, safe="/:?#[]@!$&'()*+,;=%~")
        super().__init__(
            status_code=status_code,
            body=b"",
            content_type=MIME_TEXT_PLAIN,
            headers=hdrs,
        )


class StreamingResponse(Response):
    """Streaming response for large payloads.

    `content` may be an async iterator/iterable **or** a plain sync
    iterable (e.g. a generator). A sync iterable is wrapped so the
    response always exposes an async stream; both forms are accepted.

    Usage::

        from veloce import StreamingResponse

        async def handler(request):
            def chunks():
                yield b"part-1"
                yield b"part-2"

            return StreamingResponse(chunks(), content_type="text/plain")
    """

    __slots__ = ()

    def __init__(
        self,
        content: Any,
        status_code: int = HTTP_200_OK,
        content_type: str = MIME_OCTET,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            body=b"",
            content_type=content_type,
            headers=headers,
        )
        if hasattr(content, "__aiter__"):
            self._stream: AsyncIterator[bytes] = content
        else:
            self._stream = self._aiter_sync(content)

    @staticmethod
    async def _aiter_sync(iterable: Any) -> AsyncIterator[bytes]:
        """Adapt a synchronous iterable into an async iterator.

        `str` chunks are encoded to UTF-8 so downstream byte-only paths
        (chunked transfer encoding) work uniformly.
        """
        for chunk in iterable:
            yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk

    def encode(self, keep_alive: bool = True) -> bytes:
        """For streaming, encode headers with chunked transfer."""
        return self._encode_streaming_head(
            {
                HEADER_CONTENT_TYPE: self.content_type,
                HEADER_TRANSFER_ENCODING: HEADER_VALUE_CHUNKED,
            },
            keep_alive,
        )

    async def stream_to(
        self,
        transport: Any,
        drain: Callable[[], Awaitable[None]] | None = None,
        keep_alive: bool = True,
    ) -> None:
        """Stream chunks to transport.

        When `drain` is supplied (the raw serving protocol passes its write-side
        flow-control awaitable) it is awaited after each chunk, so a producer
        outrunning a slow client is throttled instead of growing the transport
        write buffer without bound. `drain` is a no-op until the buffer crosses
        the high-water mark, so the fast path pays one already-set check.
        """
        transport.write(self.encode(keep_alive=keep_alive))
        await _write_chunked(transport, self._stream, drain)


# Files at or below this size are read inline on the event loop by
# `FileResponse.from_path`; larger files are read in a thread-pool executor so a
# big read never stalls the loop. A thread-pool hop costs ~100 us (measured),
# which dominates the few-microsecond read of a small static asset (HTML, CSS,
# JS, JSON, favicons), so paying it there is a net loss. The 64 KiB cutoff keeps
# the worst-case inline read (a cold-cache disk seek) sub-millisecond while
# covering the vast majority of per-request static assets; anything larger,
# where the offload's loop-protection actually matters, still goes to the pool.
_INLINE_READ_MAX = 64 * 1024


def _stat_regular_file(path: str) -> os.stat_result:
    """Stat `path`, raising `FileNotFoundError` for a missing or non-regular file.

    One stat covers the existence/regular-file guard and the mtime/size used for
    `Last-Modified` + `ETag`; a separate `os.path.isfile` pre-check would stat
    twice. A non-regular path (directory, broken symlink) is rejected with the
    same error a `False` from `os.path.isfile` would have produced.
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
) -> tuple[str, dict[str, str]]:
    """Resolve the content type and build the file-serving response headers.

    Shared by the sync `FileResponse(...)` and the async `from_path(...)` so the
    `Content-Disposition` / `Last-Modified` / `ETag` shape stays identical on
    both paths.
    """
    if content_type is None:
        content_type = guess_content_type(path)

    hdrs = headers or {}
    if filename:
        # `content_disposition_type` - "attachment" (force a download dialog)
        # or "inline" (render in the browser).
        hdrs[HEADER_CONTENT_DISPOSITION] = _format_content_disposition(
            content_disposition_type, filename
        )
    elif content_disposition_type != HEADER_VALUE_ATTACHMENT:
        # No filename, but the caller explicitly chose a non-default disposition
        # (e.g. `inline`): honour it with a bare `Content-Disposition: inline`.
        # The default `attachment` stays unset without a filename, so a plain
        # `FileResponse(path)` does not force a download on every file it serves.
        hdrs[HEADER_CONTENT_DISPOSITION] = content_disposition_type

    if HEADER_LAST_MODIFIED not in hdrs and "last-modified" not in hdrs:
        hdrs[HEADER_LAST_MODIFIED] = http_date(st.st_mtime)
    if HEADER_ETAG not in hdrs and "etag" not in hdrs:
        hdrs[HEADER_ETAG] = _file_etag(path, st.st_size, st.st_mtime)
    return content_type, hdrs


class FileResponse(Response):
    """Serve a file from disk - small files inline, large files via executor."""

    __slots__ = ()

    def __init__(
        self,
        path: str,
        filename: str | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
        content_disposition_type: str = HEADER_VALUE_ATTACHMENT,
    ) -> None:
        st = _stat_regular_file(path)
        content_type, hdrs = _build_file_headers(
            path, st, filename, content_type, content_disposition_type, headers
        )

        # Warn when called on a running loop - a 50 MB read on the loop
        # pauses every other request. The cheap factory
        # `await FileResponse.from_path(path)` streams the file through
        # `loop.run_in_executor` without blocking. We emit a
        # DeprecationWarning instead of raising so the established sync
        # helpers (`send_file`, `Veloce.send_static_file`) keep working
        # for now; the next major bump will tighten this to a hard error.
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
        with open(path, "rb") as f:
            body = f.read()

        super().__init__(
            status_code=HTTP_200_OK,
            body=body,
            content_type=content_type,
            headers=hdrs,
        )

    async def stream_to(
        self,
        transport: Any,
        drain: Callable[[], Awaitable[None]] | None = None,
        keep_alive: bool = True,
    ) -> None:
        """Write the head, then the file's chunks, on the raw transport.

        A file's length is known, so the body is normally length-delimited and
        not chunk-framed - which is why this does not go through
        `StreamingResponse.stream_to`.

        That holds only while the head still carries the `Content-Length`
        `from_path` set. A response middleware may legitimately remove it:
        `CompressionMiddleware` does for every streamed body, because the
        compressed length is not the length that was stat'd. `encode()` then
        falls back to `len(self.body)`, which is `0` here, and the head went out
        declaring `Content-Length: 0` in front of the body bytes - which a
        keep-alive client reads as the start of the next response.

        With no declared length there is nothing to delimit the body, so it is
        chunk-framed, exactly as `StreamingResponse` frames a body whose length
        it does not know. `drain` throttles a producer outrunning a slow client
        in both cases.
        """
        if header_get(self.headers, HEADER_CONTENT_LENGTH) is None:
            # Through `_encode_streaming_head`, as every other streaming
            # response does: `encode()` would default a `Content-Length` from
            # `len(self.body)` and emit `Content-Length: 0` beside the chunked
            # framing, which is the same contradiction in a new place.
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
    ) -> FileResponse:
        """Async factory - reads small files inline, large files in the executor.

        Stats the path on the loop (one fast syscall) to size the file. A file at
        or below `_INLINE_READ_MAX` is read inline, skipping the thread-pool hop
        that otherwise dominates serving a small static asset; a larger file is
        read in the executor so a big read never stalls the loop.
        """
        loop = asyncio.get_running_loop()

        st = _stat_regular_file(path)

        content_type, hdrs = _build_file_headers(
            path, st, filename, content_type, content_disposition_type, headers
        )

        if st.st_size <= _INLINE_READ_MAX:
            # ASYNC230: a bounded inline read is deliberate here - the file is
            # known to be <= 64 KiB, so this read is microseconds and avoids the
            # ~100 us thread-pool hop (measured) that dominates serving a small
            # asset.
            with open(path, "rb") as f:  # noqa: ASYNC230
                body = f.read()
            resp = Response.__new__(cls)
            Response.__init__(
                resp, status_code=HTTP_200_OK, body=body, content_type=content_type, headers=hdrs
            )
            return resp

        # A larger file is streamed off disk rather than read whole. Reading it
        # whole made resident memory scale with (file size x concurrent
        # requests) - four concurrent 32 MiB downloads measured at 134 MB RSS -
        # which needs no malice to hurt, only ordinary traffic. The length is
        # known from the stat, so the response stays length-delimited: this
        # streams a known-size body, it does not switch to chunked encoding.
        resp = Response.__new__(cls)
        Response.__init__(
            resp, status_code=HTTP_200_OK, body=b"", content_type=content_type, headers=hdrs
        )
        resp.headers[HEADER_CONTENT_LENGTH] = str(st.st_size)
        resp._stream = _stream_file(path, loop)
        return resp
