"""Response types — optimized serialization over the configured JSON backend."""

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

import veloce._json as _json
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
from veloce._protocol_constants import (
    AUTH_SCHEME_BASIC,
    HTTP_METHOD_GET,
    SET_COOKIE_JOINER,
)
from veloce._warnings import VeloceDeprecationWarning
from veloce.background import coerce_background
from veloce.http.cache_control import CacheControl
from veloce.http.cookies import dump_cookie, iter_cookies
from veloce.http.dates import http_date, parse_date
from veloce.http.header_set import HeaderSet
from veloce.status import (
    HTTP_200_OK,
    HTTP_206_PARTIAL_CONTENT,
    HTTP_304_NOT_MODIFIED,
    HTTP_307_TEMPORARY_REDIRECT,
    HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
    status_permits_body,
)

if TYPE_CHECKING:  # pragma: no cover
    # Type-only: a runtime import closes a response->request->exceptions cycle.
    from veloce.http.request import Request

# ── Case-insensitive response-header access ──────────────────────────
# `Response.headers` is case-SENSITIVE but HTTP field names are not (RFC 9110
# Sec. 5.1), so these fold the lookup without allocating a dict per call.


def header_key(headers: Mapping[str, str], name: str) -> str | None:
    """Return the actual stored key matching `name` case-insensitively, or None.

    Pass `name` in its canonical casing. Use the returned key to rewrite a
    value in place under whatever casing the caller originally stored.
    """
    if name in headers:
        return name
    lowered = name.lower()
    # A first-byte guard before the `.lower()` measured slower on a short dict.
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

    Costs one dict lookup for the canonical casing, which is what the framework
    itself always writes.
    """
    key = header_key(headers, name)
    return None if key is None else headers.pop(key)


def _format_content_disposition(disposition: str, filename: str) -> str:
    """Build a safe RFC 6266 ``Content-Disposition`` header value.

    An ASCII name made only of RFC 9110 quoted-string members (HTAB, SP,
    0x21-0x7E) is emitted as ``filename="..."``; any other name is emitted only
    as the RFC 5987 ``filename*=UTF-8''...`` form, with no lossy legacy slot. A
    CR/LF in the name is rejected so it cannot inject a header.
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


#: Bytes per chunk off disk: amortises the executor hop, bounds the download.
FILE_STREAM_CHUNK = 64 * 1024


def _read_file_chunk(handle: BinaryIO) -> bytes:
    """Read one chunk from an open file - run in an executor."""
    return handle.read(FILE_STREAM_CHUNK)


def advertised_length(status_code: int, body: bytes) -> int | None:
    """Return the `Content-Length` to synthesize, or `None` when it is unknown.

    A body-permitting status advertises what it carries. A 304 may advertise the
    length the equivalent 200 would have carried (RFC 9110 Sec. 8.6 / 15.4.5),
    but only when that is knowable; an unknown length is omitted rather than
    reported as 0, because RFC 9111 Sec. 4.3.4 has caches write a 304's length
    over their stored entry and a guessed 0 records a 200 KiB asset as empty.
    Every other bodiless status (1xx, 204, 205) has no representation, so its
    length is 0.
    """
    if status_permits_body(status_code):
        return len(body)
    if status_code == HTTP_304_NOT_MODIFIED:
        return len(body) or None
    return 0


# Merged `Vary` values keyed by `(existing_header, names_added)`; a pure merge.
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

    #: An SSE stream. A class attribute: a `getattr` default on slots costs ~38 ns.
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
        # Copied: a shared `HEADERS = {...}` would ship one user's session cookie on.
        self.headers = dict(headers) if headers else {}
        # Coerced, not stored raw: the dispatch cascade knows only `run` / `run_all`,
        # so another value would serve as if the task had run. The `None`
        # comparison measured 72 ns of a 432 ns `Response()`.
        self.background = None if background is None else coerce_background(background)
        # `StreamingResponse` rewrites this; `None` keeps `is_streamed` an attr load.
        self._stream: Any = None
        # `content_type` has no setter hook, so the parse cache keys on its value.
        self._ct_cache_key: str | None = None
        self._mimetype: str = ""
        self._charset: str = ""
        self._ct_params: dict[str, str] = {}

    # ── `body` ────────────────────────────────────────────────
    # Backed by `_body` so the setter invalidates the encode cache: a middleware
    # assigning after an `.encode()` would otherwise emit stale bytes.

    @property
    def body(self) -> bytes:
        """Return the response body bytes."""
        return self._body

    @body.setter
    def body(self, value: bytes) -> None:
        """Set the response body, keeping any `Content-Length` in step.

        The single assignment `body`, `data` and `set_data` all go through, so
        no spelling can advertise a stale length and desynchronise a keep-alive
        connection. The constructor assigns `_body` directly, so a response that
        never reassigns its body pays nothing.
        """
        self._body = value
        stored = header_key(self.headers, HEADER_CONTENT_LENGTH)
        if stored is not None:
            self.headers[stored] = str(len(value))
        self._encoded = None

    # ── `media_type` alias ────────────────────────────────────
    # ASGI servers name this attribute `media_type`; veloce's canonical name is
    # `content_type`. Both spellings read and write cleanly.

    @property
    def media_type(self) -> str:
        """Return the full Content-Type including parameters."""
        return self.content_type

    @media_type.setter
    def media_type(self, value: str) -> None:
        """Set the media type."""
        self.content_type = value
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
        return _json.loads(body) if body else None

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
        params = dict(parse_media_type_params(rest)) if semi else {}
        self._ct_params = params
        # Off the shared parser, so the charset honours RFC 9110 Sec. 8.3.1 and 5.6.6.
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
        # Read off the parsed map, so a `Charset=` carries (RFC 9110 Sec. 8.3.1).
        cs = self.charset
        had_charset = "charset" in self.mimetype_params
        self.content_type = f"{value}; charset={cs}" if had_charset else value
        self._encoded = None

    # ── `status` line ─────────────────────────────────────────
    # `status_code` is the canonical field; `status` is its string view.

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

        `keep_alive` is what the transport decided about this connection. Only
        the keep-alive encode is cached, so a response re-encoded for a closing
        connection cannot serve a stale `Connection` line.
        """
        if keep_alive and self._encoded is not None:
            return self._encoded

        # Bodiless statuses (1xx/204/205/304) get no default content-type; a
        # handler-set one still wins. A 304 may advertise the would-be-200
        # `Content-Length` (RFC 9110 Sec. 8.6), while 1xx/204/205 advertise 0.
        body_allowed = status_permits_body(self.status_code)
        body = self.body if body_allowed else b""
        length = advertised_length(self.status_code, self.body)
        default_headers = {} if length is None else {HEADER_CONTENT_LENGTH: str(length)}
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
        max_age: int | timedelta | None = None,
        expires: int | float | datetime | date | str | None = None,
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

        `partitioned=True` adds the CHIPS `Partitioned` attribute, which keys
        the cookie to the top-level site so embedded third-party contexts each
        get an isolated jar. It requires `Secure`, so it is emitted only when
        `secure=True`.

        `prefix="host"` / `prefix="secure"` add the RFC 6265bis Sec. 4.1.3
        name prefix (`__Host-` / `__Secure-`) and enforce its invariants:
        `"secure"` requires `secure=True`; `"host"` also requires `path="/"`
        and no `domain`. A violation raises `ValueError`.

        The name, value, domain, path and samesite fields are rejected if they
        contain CR, LF, or NUL - untrusted data must not be able to inject
        additional cookies or response headers.
        """
        # `dump_cookie` takes datetime and numeric stamps, not formatted strings.
        expires_str: str | None = None
        dump_expires: int | float | datetime | date | None = None
        if isinstance(expires, str):
            expires_str = expires
        else:
            dump_expires = expires

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

        The emit paths compute `Content-Length` from `body` anyway; this is for
        callers that want it locked into `self.headers` ahead of time.
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
        Naive datetimes are interpreted as UTC.
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

        Values are percent-decoded, so a cookie reads back as the string
        `set_cookie()` was given rather than its wire form, and a repeated name
        resolves to the last set - as the client resolves it. Introspection
        only; mutation goes through `set_cookie()`.
        """
        out: dict[str, str] = {}
        existing = header_get(self.headers, HEADER_SET_COOKIE) or ""
        if not existing:
            return out
        # Only the leading `name=value` is the cookie; `iter_cookies` inverts
        # `dump_cookie`.
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
                # The f-string label is built only on the path that raises.
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
        """Body bytes alias for `Response.body`."""
        return self.body

    @data.setter
    def data(self, value: bytes | str) -> None:
        """Set the response body; alias for `set_data`."""
        self.set_data(value)

    def set_data(self, value: bytes | str) -> None:
        """Replace the response body.

        Accepts `bytes` or `str` (UTF-8 encoded). Refreshes `Content-Length`
        when the headers carry one. Assigning `response.body` does the same.
        """
        if isinstance(value, str):
            value = value.encode("utf-8")
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
        # Every casing, not just `Vary` and `vary`: both emit paths keep the last
        # write, and a `Vary: Cookie` a shared cache never sees is how one
        # user's response gets served to another.
        stored_key = header_key(headers, HEADER_VARY)
        # A lone clean token with no existing `Vary` round-trips; skip the parse.
        if len(header_names) == 1 and stored_key is None:
            value = header_names[0]
            headers[HEADER_VARY] = value
            self._encoded = None
            return value
        existing = headers[stored_key] if stored_key is not None else ""
        # Pure, inputs fixed per app. Measured 5.3 us against 0.5 us on the fast path.
        cache_key = (existing, header_names)
        cached = _VARY_MERGES.get(cache_key)
        if cached is not None:
            value = cached
        else:
            # Through `HeaderSet`, so the merge cannot drift from the `vary` property.
            merged = HeaderSet(existing)
            merged.update(header_names)
            value = merged.to_header()
            # Bounded: a handler may write `response.headers["Vary"]` from input.
            if len(_VARY_MERGES) >= _MAX_VARY_MERGES:
                _VARY_MERGES.clear()
            _VARY_MERGES[cache_key] = value
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
            # A challenge may interpolate a realm or token68, so CRLF is rejected.
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

        Pick the loop based on `response.is_streamed`, as `iter_encoded` shows.
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
        # Type-only, because `serving/` must not import `sse`; no runtime method.
        async def stream_to(
            self,
            transport: Any,
            drain: Callable[[], Awaitable[None]] | None = None,
            keep_alive: bool = True,
        ) -> None: ...

    def _encode_streaming_head(self, default_headers: dict[str, str], keep_alive: bool) -> bytes:
        """Encode a streaming response's head, honouring the bodiless-status rule.

        RFC 9112 Sec. 6.1 forbids `Transfer-Encoding` on a 204, and a 204 that
        ships chunks desynchronises a keep-alive connection. `default_headers`
        is therefore dropped for `Content-Length: 0` on a bodiless status.
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
        `weak=True` prepends `W/`. Overwrites any `ETag` already set; pass the
        explicit one in `__init__(headers=...)` to skip this. Returns the value.
        """
        # `usedforsecurity=False`: an opaque cache validator must not raise on FIPS.
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
        # Field names are case-insensitive (RFC 9110 Sec. 5.1), so `Etag` is honored.
        ours_etag = header_get(self.headers, HEADER_ETAG) or ""
        inm = getattr(request, "if_none_match", ())
        if inm:
            # With no ETag it is not a match, and Sec. 13.2 still ranks it first.
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
            # RFC 9110 Sec. 13.2.2: evaluated only when `If-Match` is absent.
            return self._check_unmodified_since(request)
        # `If-Match: *` is an existence precondition (RFC 9110 Sec. 13.1.1).
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
            # Nothing to compare, so this is not a reason to refuse.
            return self
        parsed = parse_date(raw)
        # Whole seconds: an HTTP-date carries no finer resolution.
        if parsed is None or int(parsed.timestamp()) <= int(since):
            return self
        from veloce.exceptions import PreconditionFailed  # response <-> exceptions cycle

        raise PreconditionFailed

    def _downgrade_to_304(self) -> None:
        """Strip body + flip status to 304. Used by `make_conditional`."""
        # RFC 9110 Sec. 8.6: a 304 may carry the `Content-Length` a 200 would have,
        # so it is recorded before the body is dropped - a zero recomputed from
        # the emptied body is written over the cache's stored length (RFC 9111
        # Sec. 4.3.4). Idempotent: two callers must not re-record that zero.
        if self.status_code == HTTP_304_NOT_MODIFIED:
            return
        # A streamed length comes from the producer's header; unknown is omitted.
        declared = header_get(self.headers, HEADER_CONTENT_LENGTH)
        representation_length = declared if self._stream is not None else str(len(self.body))
        self.status_code = HTTP_304_NOT_MODIFIED
        self.body = b""
        # A 304 carries no content, so the stream drops with the body.
        self._stream = None
        header_pop(self.headers, HEADER_CONTENT_LENGTH)
        if representation_length is not None:
            self.headers[HEADER_CONTENT_LENGTH] = representation_length
        self._encoded = None

    # ── Content-Disposition and cookie removal ────────────────

    def set_content_disposition(
        self, disposition: str = HEADER_VALUE_ATTACHMENT, filename: str | None = None
    ) -> str:
        """Write a `Content-Disposition` header - RFC 6266.

        `disposition` is `"attachment"` (force download) or `"inline"` (render
        in-browser). A quotable ASCII `filename` uses `filename="..."`; any
        other name uses only the RFC 5987 `filename*=UTF-8''...` form. Returns
        the header value written.
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
    """JSON response encoded by the configured JSON backend.

    Usage::

        from veloce import JSONResponse

        async def handler(request):
            return JSONResponse({"ok": True}, status_code=200)
    """

    __slots__ = ()

    # Class default Content-Type, named apart from `Response.media_type`.
    default_media_type: str = MIME_JSON

    def __init__(
        self,
        data: Any,
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
        background: Any = None,
    ) -> None:
        try:
            # Through the app's provider, so `JSON_SORT_KEYS` reaches this class.
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
        path produced these bytes itself, so it needs neither the check nor a
        second encoder lookup.
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

        Skips `__init__`'s re-encode - use this when the caller has
        produced the JSON body itself. The body is sent verbatim with
        `Content-Type` from `cls.default_media_type`, unless `headers` carries a
        `Content-Type`, in which case the caller-supplied value wins.

        The caller is responsible for `body` being valid UTF-8 JSON; nothing is
        parsed or validated. `body` must be `bytes` or `bytearray` - a `str`
        raises `TypeError` rather than being encoded under an assumed charset.
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
    """Semantic alias for `JSONResponse`.

    Both encode through the configured JSON backend, which is selected at
    import rather than by the response class.
    """

    __slots__ = ()


class UJSONResponse(Response):
    """JSON response encoded with `ujson`.

    Imports `ujson` at construction, so apps that do not use this class do not
    need the package installed.
    """

    __slots__ = ()

    def __init__(
        self,
        data: Any,
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        try:
            import ujson
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
        # Reject CR/LF and percent-encode, so a crafted URL or Host header cannot
        # inject response headers. The safe set keeps RFC 3986 structural
        # characters and `%`, so an encoded URL is not double-encoded.
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
        # Both branches go through an adapter that encodes `str`.
        self._stream: AsyncIterator[bytes] | None = (
            self._aiter_async(content)
            if hasattr(content, "__aiter__")
            else self._aiter_sync(content)
        )

    @staticmethod
    async def _aiter_sync(iterable: Any) -> AsyncIterator[bytes]:
        """Adapt a synchronous iterable into an async iterator.

        `str` chunks are encoded to UTF-8 so downstream byte-only paths
        (chunked transfer encoding) work uniformly.
        """
        for chunk in iterable:
            yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk

    @staticmethod
    async def _aiter_async(iterable: Any) -> AsyncIterator[bytes]:
        """Adapt an async iterable, encoding `str` chunks the way `_aiter_sync` does."""
        async for chunk in iterable:
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
        # A 304 drops the stream with the body (RFC 9110 Sec. 15.4.5).
        if self._stream is not None:
            await _write_chunked(transport, self._stream, drain)


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
        """
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
            with open(path, "rb") as f:  # noqa: ASYNC230
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
