"""Response types - optimized serialization with orjson."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import quote

import orjson

from veloce._constants import (
    HEADER_ACCEPT_RANGES,
    HEADER_AGE,
    HEADER_ALLOW,
    HEADER_CACHE_CONTROL,
    HEADER_CONNECTION,
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
    HEADER_VALUE_KEEP_ALIVE,
    HEADER_VALUE_NO_CACHE,
    HEADER_VALUE_PUBLIC,
    HEADER_VARY,
    HEADER_WWW_AUTHENTICATE,
    MIME_TEXT_PLAIN,
)
from veloce._internal import (
    _STATUS_PHRASES,
    MIME_HTML,
    MIME_JSON,
    MIME_OCTET,
    MIME_PLAIN,
    _encode_response_head,
    _etag_matches_strong,
    _etag_matches_weak,
    _file_etag,
    _reject_header_crlf,
)
from veloce._protocol_constants import AUTH_SCHEME_BASIC, SET_COOKIE_JOINER
from veloce.encoders import orjson_default
from veloce.http.cache_control import CacheControl
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


class Response:
    """Base HTTP response."""

    __slots__ = (
        "status_code",
        "_body",
        "content_type",
        "headers",
        "_encoded",
        "background",
        "_stream",
    )

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
        self.headers = headers or {}
        # Optional `BackgroundTask` or `BackgroundTasks` fired by the
        # dispatch layer after this response is built. None when no task
        # is attached. `Response(content=..., background=BackgroundTask(fn))`.
        self.background = background
        # `StreamingResponse` rewrites this with an async iterator; for a
        # base `Response` the slot stays `None` so `is_streamed` is a
        # direct attribute load (no `getattr` fallback to None).
        self._stream: Any = None

    # -- `body` -------------------------------------------------------
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
        """Set the response body."""
        self._body = value
        self._encoded = None

    # -- `media_type` alias --------------------------------------------
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

    # -- `mimetype` ---------------------------------------------------
    # `mimetype` is the bare media type, with no parameters.
    # Setting it preserves the existing `charset` parameter.

    @property
    def is_json(self) -> bool:
        """True when `Content-Type` is JSON.

        Matches `application/json` and any `application/*+json`
        structured suffix (RFC 6839 Sec. 3.1).
        """
        mt = self.mimetype
        if mt == MIME_JSON:
            return True
        return mt.startswith("application/") and mt.endswith("+json")

    def get_json(self) -> Any:
        """Parse the response body as JSON.

        Returns `None` for an empty body. Useful in tests to inspect a
        JSON response without re-decoding `body` by hand. Raises if the
        body is non-empty and not valid JSON.
        """
        body = self.body
        return orjson.loads(body) if body else None

    @property
    def mimetype(self) -> str:
        """The bare media type - `Content-Type` without parameters.

        `text/html; charset=utf-8` -> `text/html`. Lower-cased and
        stripped per RFC 9110 Sec. 8.3 (media types are case-insensitive).
        """
        return (self.content_type or "").split(";", 1)[0].strip().lower()

    @mimetype.setter
    def mimetype(self, value: str) -> None:
        """Set the mimetype."""
        # Preserve the current charset parameter, if any.
        cs = self.charset
        ct = self.content_type or ""
        had_charset = "charset=" in ct
        self.content_type = f"{value}; charset={cs}" if had_charset else value
        self._encoded = None

    # -- `status` line ------------------------------------------------
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

    def encode(self) -> bytes:
        """Encode to raw HTTP/1.1 bytes - called once, cached."""
        if self._encoded is not None:
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
        default_headers = {
            HEADER_CONTENT_LENGTH: str(advertised_length),
            HEADER_CONNECTION: HEADER_VALUE_KEEP_ALIVE,
        }
        if body_allowed:
            default_headers = {HEADER_CONTENT_TYPE: self.content_type, **default_headers}
        parts = _encode_response_head(self.status_code, default_headers, self.headers)
        parts.append("\r\n")

        self._encoded = "".join(parts).encode("latin-1") + body
        return self._encoded

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
        # Normalise empty samesite to None so dump_cookie omits it.
        if samesite is not None and not samesite.strip():
            samesite = None

        # dump_cookie accepts datetime and numeric timestamps but not
        # pre-formatted strings. Handle the string case separately.
        expires_str: str | None = None
        dump_expires = expires
        if isinstance(expires, str):
            expires_str = expires
            dump_expires = None

        from veloce.http.cookies import dump_cookie  # breaks http.response -> http.cookies cycle

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
        """Append `raw_value` (already a serialised Set-Cookie line, or
        a `\\r\\nSet-Cookie: `-joined multi-cookie blob) onto the response's
        Set-Cookie header without overwriting earlier cookies. Single
        canonical home for the Q44 multi-cookie join format.
        """
        existing = self.headers.get(HEADER_SET_COOKIE)
        if existing:
            self.headers[HEADER_SET_COOKIE] = existing + SET_COOKIE_JOINER + raw_value
        else:
            self.headers[HEADER_SET_COOKIE] = raw_value

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
        ct = self.content_type or ""
        for part in ct.split(";"):
            part = part.strip()
            if part.startswith("charset="):
                return part[8:].strip().strip('"')
        return "utf-8"

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
        params: dict[str, str] = {}
        ct = self.content_type or ""
        for part in ct.split(";")[1:]:
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            params[key.strip().lower()] = value.strip().strip('"')
        return params

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

    @property
    def last_modified(self) -> Any:
        """Parsed `Last-Modified` header -> UTC `datetime` or None.

        Accepts the three RFC 9110 Sec. 5.6.7 HTTP-date
        forms. Returns `None` on missing/unparseable.
        """
        raw = self.headers.get(HEADER_LAST_MODIFIED) or self.headers.get("last-modified")
        if not raw:
            return None
        return parse_date(raw)

    @last_modified.setter
    def last_modified(self, value: Any) -> None:
        """Set the last modified date."""
        self._set_http_date_header(HEADER_LAST_MODIFIED, value)

    @property
    def expires(self) -> Any:
        """Parsed `Expires` header -> UTC `datetime` or None (RFC 9111 Sec. 5.3)."""
        raw = self.headers.get(HEADER_EXPIRES) or self.headers.get("expires")
        if not raw:
            return None
        return parse_date(raw)

    @expires.setter
    def expires(self, value: Any) -> None:
        """Set the expires date."""
        self._set_http_date_header(HEADER_EXPIRES, value)

    def _set_http_date_header(self, name: str, value: Any) -> None:
        """Set an HTTP-date header from datetime / unix ts / preformatted str.

        `value=None` removes the header (both canonical and lower-case
        variants). Naive datetimes are interpreted as UTC.
        """
        if value is None:
            self.headers.pop(name, None)
            self.headers.pop(name.lower(), None)
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

        Walks every `Set-Cookie` entry (Q44 separator `\\r\\nSet-Cookie: `
        respected) and returns `{name: value}`. Multiple cookies with
        the same name resolve to the last set - matches the wire
        behaviour where the client also keeps the most-recent value.
        Caller introspection only; mutation goes through `set_cookie()`.
        """
        out: dict[str, str] = {}
        existing = self.headers.get(HEADER_SET_COOKIE, "") or self.headers.get("set-cookie", "")
        if not existing:
            return out
        # Q44 emits multi-cookies as `cookie1\r\nSet-Cookie: cookie2...`.
        for line in existing.split(SET_COOKIE_JOINER):
            first = line.split(";", 1)[0].strip()
            if "=" in first:
                name, _, value = first.partition("=")
                out[name.strip()] = value.strip()
        return out

    @property
    def headerlist(self) -> list[tuple[str, str]]:
        """Headers flattened to a `(name, value)` tuple list.

        Each `Set-Cookie` (Q44 multi-cookie join) expands to its own
        tuple, so downstream wire-emit / inspection code gets the
        per-cookie view ASGI requires.
        """
        result: list[tuple[str, str]] = []
        for k, v in self.headers.items():
            if k.lower() == "set-cookie":
                for piece in v.split(SET_COOKIE_JOINER):
                    result.append((k, piece.strip()))
            else:
                result.append((k, v))
        return result

    @property
    def data(self) -> bytes:
        """Body bytes alias for `Response.body`.

        Read returns the current body; writing through the setter
        replaces the body, invalidates any cached HTTP/1.1 encoded
        bytes (`_encoded`), and updates `Content-Length` on the
        headers if it was previously set.
        """
        return self.body

    @data.setter
    def data(self, value: bytes | str) -> None:
        """Set the data."""
        self.set_data(value)

    def set_data(self, value: bytes | str) -> None:
        """Replace the response body.

        Accepts `bytes` or `str` (UTF-8 encoded). Invalidates the cached
        HTTP/1.1 encode so the new body wire-out on the next emit.
        Refreshes `Content-Length` when previously set on the headers.
        """
        if isinstance(value, str):
            value = value.encode("utf-8")
        # The `body` property setter clears `_encoded`; no separate
        # invalidation needed.
        self.body = value
        # If `Content-Length` was explicitly set (e.g. by caller after
        # the prior body), refresh it to match. The ASGI emit path
        # always recomputes Content-Length from `body`, so leaving
        # the header stale would only affect the raw HTTP/1.1 encode
        # path. Keep both consistent.
        for key in (HEADER_CONTENT_LENGTH, "content-length"):
            if key in self.headers:
                self.headers[key] = str(len(value))

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
        # Delegate dedup + ordering to `HeaderSet` so the same
        # case-insensitive merge logic doesn't drift between this method
        # and the `vary` property's own datastructure.
        existing = self.headers.get(HEADER_VARY, "") or self.headers.get("vary", "")
        merged = HeaderSet(existing)
        merged.update(header_names)
        value = merged.to_header()
        # Always write under `Vary` (canonical case) and clear any
        # lower-case duplicate.
        self.headers.pop("vary", None)
        self.headers[HEADER_VARY] = value
        self._encoded = None
        return value

    @property
    def vary(self) -> Any:
        """The `Vary` header as a `HeaderSet`.

        Returns a fresh `HeaderSet` parsed from the current header.
        Assign a `HeaderSet`, iterable of strings, or a comma-separated
        string to replace it. Mutating the returned object does *not*
        write back - call `add_vary(...)` or reassign for that.
        """

        return HeaderSet(self.headers.get(HEADER_VARY, ""))

    @vary.setter
    def vary(self, value: Any) -> None:
        """Set the vary."""
        hs = value if isinstance(value, HeaderSet) else HeaderSet(value)
        self.headers.pop("vary", None)
        self.headers[HEADER_VARY] = hs.to_header()
        self._encoded = None

    @property
    def allow(self) -> Any:
        """The `Allow` header as a `HeaderSet`.

        Lists the HTTP methods the resource supports (RFC 9110 Sec. 10.2.1).
        Assign a `HeaderSet`, iterable, or comma-separated string.
        """

        return HeaderSet(self.headers.get(HEADER_ALLOW, ""))

    @allow.setter
    def allow(self, value: Any) -> None:
        """Set the allow."""
        hs = value if isinstance(value, HeaderSet) else HeaderSet(value)
        self.headers.pop("allow", None)
        self.headers[HEADER_ALLOW] = hs.to_header()
        self._encoded = None

    @property
    def www_authenticate(self) -> str | None:
        """The `WWW-Authenticate` challenge header - RFC 9110 Sec. 11.6.1.

        Sent on `401 Unauthorized` to tell the client which auth
        scheme(s) to use. `None` when unset.
        """
        return self.headers.get(HEADER_WWW_AUTHENTICATE)

    @www_authenticate.setter
    def www_authenticate(self, value: str | None) -> None:
        """Set the WWW-Authenticate."""
        if value is None:
            self.headers.pop(HEADER_WWW_AUTHENTICATE, None)
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

    @property
    def content_encoding(self) -> str | None:
        """The `Content-Encoding` header - RFC 9110 Sec. 8.4. `None` when unset."""
        return self.headers.get(HEADER_CONTENT_ENCODING)

    @content_encoding.setter
    def content_encoding(self, value: str | None) -> None:
        """Set the content encoding."""
        if value is None:
            self.headers.pop(HEADER_CONTENT_ENCODING, None)
        else:
            self.headers[HEADER_CONTENT_ENCODING] = value
        self._encoded = None

    @property
    def content_language(self) -> str | None:
        """The `Content-Language` header - RFC 9110 Sec. 8.5. `None` when unset."""
        return self.headers.get(HEADER_CONTENT_LANGUAGE)

    @content_language.setter
    def content_language(self, value: str | None) -> None:
        """Set the content language."""
        if value is None:
            self.headers.pop(HEADER_CONTENT_LANGUAGE, None)
        else:
            self.headers[HEADER_CONTENT_LANGUAGE] = value
        self._encoded = None

    @property
    def accept_ranges(self) -> str | None:
        """The `Accept-Ranges` header - RFC 9110 Sec. 14.3.

        Typically `bytes` (range requests supported) or `none`
        (explicitly unsupported). `None` when the header is unset.
        """
        return self.headers.get(HEADER_ACCEPT_RANGES)

    @accept_ranges.setter
    def accept_ranges(self, value: str | None) -> None:
        """Set the accept ranges."""
        if value is None:
            self.headers.pop(HEADER_ACCEPT_RANGES, None)
        else:
            self.headers[HEADER_ACCEPT_RANGES] = value
        self._encoded = None

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
        return self.headers.get(HEADER_CONTENT_RANGE)

    @property
    def date(self) -> Any:
        """The `Date` header as a tz-aware UTC `datetime` - RFC 9110 Sec. 6.6.1.

        Returns `None` when unset or unparseable. Assign a `datetime`
        or POSIX timestamp to set it; assign `None` to remove it.
        """

        return parse_date(self.headers.get(HEADER_DATE))

    @date.setter
    def date(self, value: Any) -> None:
        """Set the date."""
        if value is None:
            self.headers.pop(HEADER_DATE, None)
        else:
            self.headers[HEADER_DATE] = http_date(value)
        self._encoded = None

    @property
    def location(self) -> str | None:
        """The `Location` header - RFC 9110 Sec. 10.2.2. `None` when unset."""
        return self.headers.get(HEADER_LOCATION)

    @location.setter
    def location(self, value: str | None) -> None:
        """Set the location."""
        if value is None:
            self.headers.pop(HEADER_LOCATION, None)
        else:
            self.headers[HEADER_LOCATION] = value
        self._encoded = None

    @property
    def content_location(self) -> str | None:
        """The `Content-Location` header - RFC 9110 Sec. 8.7. `None` when unset."""
        return self.headers.get(HEADER_CONTENT_LOCATION)

    @content_location.setter
    def content_location(self, value: str | None) -> None:
        if value is None:
            self.headers.pop(HEADER_CONTENT_LOCATION, None)
        else:
            self.headers[HEADER_CONTENT_LOCATION] = value
        self._encoded = None

    @property
    def retry_after(self) -> Any:
        """The `Retry-After` header - RFC 9110 Sec. 10.2.3.

        Returns an `int` (delay in seconds) when the header is numeric,
        a tz-aware `datetime` when it's an HTTP-date, or `None` when
        unset. Assign an int / `timedelta` / `datetime` to set it;
        assign `None` to remove it.
        """
        raw = self.headers.get(HEADER_RETRY_AFTER)
        if not raw:
            return None
        raw = raw.strip()
        if raw.isdigit():
            return int(raw)

        return parse_date(raw)

    @retry_after.setter
    def retry_after(self, value: Any) -> None:
        """Set the retry after."""
        if value is None:
            self.headers.pop(HEADER_RETRY_AFTER, None)
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
        raw = self.headers.get(HEADER_AGE)
        if not raw or not raw.strip().isdigit():
            return None
        return int(raw.strip())

    @age.setter
    def age(self, value: int | None) -> None:
        if value is None:
            self.headers.pop(HEADER_AGE, None)
        else:
            self.headers[HEADER_AGE] = str(int(value))
        self._encoded = None

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
        raw = self.headers.get(HEADER_ETAG) or self.headers.get("etag")
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
    def cache_control(self) -> Any:
        """Parsed `Cache-Control` header (read-only view).

        For setting directives, prefer `set_cache_control(...)` which
        writes the header directly. This property is convenient for
        introspection: `resp.cache_control.max_age`,
        `resp.cache_control.no_store`, etc.
        """

        return CacheControl(self.headers.get(HEADER_CACHE_CONTROL, ""))

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

        The return shape is mode-dependent: a buffered response yields a
        synchronous iterator of `bytes`, a streaming response yields the
        underlying `AsyncIterator[bytes]`. Branch on `response.is_streamed`
        to drain with the right loop.
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

        `size` must be positive. The return shape is mode-dependent: branch
        on `response.is_streamed` to drain with the right loop.
        """
        if size <= 0:
            raise ValueError("iter_chunked size must be positive")
        stream = self._stream
        if stream is not None:
            return stream
        body = self.body
        return (body[i : i + size] for i in range(0, len(body), size))

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

    def make_conditional(self, request: Any) -> Response:
        """Downgrade this response to 304 when the request's preconditions
        match the response's ETag / Last-Modified.

        Checks `If-None-Match` first (per RFC 9110 Sec. 13.2 precedence),
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
        if inm and ours_etag:
            if "*" in inm:
                self._downgrade_to_304()
                return self
            # Weak comparison per RFC 9110 Sec. 8.8.3.2.
            for tag in inm:
                if _etag_matches_weak(ours_etag, tag):
                    self._downgrade_to_304()
                    return self
            # Explicit non-match - caller's other preconditions don't apply.
            return self

        # If-Modified-Since (only consulted when If-None-Match absent).
        ours_lm = header_get(self.headers, HEADER_LAST_MODIFIED) or ""
        ims = getattr(request, "if_modified_since", None)
        if ims is not None and ours_lm:
            ours_dt = parse_date(ours_lm)
            if ours_dt is None:
                return self
            ours_ts = ours_dt.timestamp()
            # HTTP-date second resolution - integer floor.
            if int(ours_ts) <= int(ims):
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
            return self
        # `If-Match: *` is an existence precondition (RFC 9110 Sec. 13.1.1):
        # the handler producing this response means the resource exists, so it
        # is satisfied regardless of whether an ETag was attached.
        if if_match == ("*",):
            return self
        # `headers` is a plain dict; accept either spelling, as other helpers do.
        ours_etag = self.headers.get(HEADER_ETAG) or self.headers.get("etag") or ""
        for tag in if_match:
            if _etag_matches_strong(ours_etag, tag):
                return self
        from veloce.exceptions import PreconditionFailed  # avoids response <-> exceptions cycle

        raise PreconditionFailed

    def _downgrade_to_304(self) -> None:
        """Strip body + flip status to 304. Used by `make_conditional`."""
        self.status_code = HTTP_304_NOT_MODIFIED
        self.body = b""
        # Content-Length removal so a 304 doesn't carry a length
        # for a body it isn't sending (RFC 9110 Sec. 15.4.5).
        self.headers.pop(HEADER_CONTENT_LENGTH, None)
        self.headers.pop("content-length", None)
        self._encoded = None

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
    """JSON response using orjson for speed."""

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
    ) -> None:
        try:
            body = orjson.dumps(data, default=orjson_default)
        except TypeError as exc:
            raise ValueError(f"JSONResponse data is not JSON-serializable: {exc}") from exc
        super().__init__(
            status_code=status_code,
            body=body,
            content_type=type(self).default_media_type,
            headers=headers,
        )

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
                "UJSONResponse requires the `ujson` package. Install it: pip install ujson"
            ) from err
        body = ujson.dumps(data).encode("utf-8")
        super().__init__(
            status_code=status_code,
            body=body,
            content_type=MIME_JSON,
            headers=headers,
        )


class HTMLResponse(Response):
    """HTML response."""

    __slots__ = ()

    def __init__(
        self,
        content: str,
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            body=content.encode("utf-8"),
            content_type=MIME_HTML,
            headers=headers,
        )


class PlainTextResponse(Response):
    """Plain text response."""

    __slots__ = ()

    def __init__(
        self,
        content: str,
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            body=content.encode("utf-8"),
            content_type=MIME_PLAIN,
            headers=headers,
        )


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

    def encode(self) -> bytes:
        """For streaming, encode headers with chunked transfer."""
        default_headers = {
            HEADER_CONTENT_TYPE: self.content_type,
            HEADER_TRANSFER_ENCODING: HEADER_VALUE_CHUNKED,
            HEADER_CONNECTION: HEADER_VALUE_KEEP_ALIVE,
        }
        parts = _encode_response_head(self.status_code, default_headers, self.headers)
        parts.append("\r\n")
        return "".join(parts).encode("latin-1")

    async def stream_to(
        self,
        transport: Any,
        drain: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Stream chunks to transport.

        When `drain` is supplied (the raw serving protocol passes its write-side
        flow-control awaitable) it is awaited after each chunk, so a producer
        outrunning a slow client is throttled instead of growing the transport
        write buffer without bound. `drain` is a no-op until the buffer crosses
        the high-water mark, so the fast path pays one already-set check.
        """
        transport.write(self.encode())
        async for chunk in self._stream:
            size = format(len(chunk), "x")
            transport.write(f"{size}\r\n".encode() + chunk + b"\r\n")
            if drain is not None:
                await drain()
        transport.write(b"0\r\n\r\n")


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
        # RFC 9110 Sec. 5.6.4 quoted-string escape: backslash first so an
        # original backslash is not doubled again when escaping the quote.
        escaped = filename.replace("\\", "\\\\").replace('"', '\\"')
        param = f'filename="{escaped}"'
    else:
        param = f"filename*=UTF-8''{quote(filename, safe='')}"
    return f"{disposition}; {param}"


class FileResponse(Response):
    """Serve a file from disk - uses async I/O via executor."""

    def __init__(
        self,
        path: str,
        filename: str | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
        content_disposition_type: str = HEADER_VALUE_ATTACHMENT,
    ) -> None:
        # Validate path exists (cheap stat check - actual read is deferred)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"File not found: {path}")

        if content_type is None:
            content_type = mimetypes.guess_type(path)[0] or MIME_OCTET

        hdrs = headers or {}
        if filename:
            # `content_disposition_type` - "attachment" (force a
            # download dialog) or "inline" (render in the browser).
            hdrs[HEADER_CONTENT_DISPOSITION] = _format_content_disposition(
                content_disposition_type, filename
            )

        st = os.stat(path)
        if HEADER_LAST_MODIFIED not in hdrs and "last-modified" not in hdrs:
            hdrs[HEADER_LAST_MODIFIED] = http_date(st.st_mtime)
        if HEADER_ETAG not in hdrs and "etag" not in hdrs:
            hdrs[HEADER_ETAG] = _file_etag(path, st.st_size, st.st_mtime)

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
            import warnings

            warnings.warn(
                "FileResponse(path) does a blocking read on the running "
                "event loop. Use `await FileResponse.from_path(path, ...)` "
                "from async handlers, or wrap the sync call in "
                "`asyncio.to_thread(...)`. This will raise in a future "
                "release.",
                DeprecationWarning,
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

    @classmethod
    async def from_path(
        cls,
        path: str,
        filename: str | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
        content_disposition_type: str = HEADER_VALUE_ATTACHMENT,
    ) -> FileResponse:
        """Async factory - reads file in executor to avoid blocking event loop."""
        loop = asyncio.get_running_loop()

        def _read_and_stat() -> tuple[bytes, os.stat_result]:
            if not os.path.isfile(path):
                raise FileNotFoundError(f"File not found: {path}")
            with open(path, "rb") as f:
                body = f.read()
                st = os.fstat(f.fileno())
            return body, st

        body, st = await loop.run_in_executor(None, _read_and_stat)

        if content_type is None:
            content_type = mimetypes.guess_type(path)[0] or MIME_OCTET

        hdrs = headers or {}
        if filename:
            hdrs[HEADER_CONTENT_DISPOSITION] = _format_content_disposition(
                content_disposition_type, filename
            )
        if HEADER_LAST_MODIFIED not in hdrs and "last-modified" not in hdrs:
            hdrs[HEADER_LAST_MODIFIED] = http_date(st.st_mtime)
        if HEADER_ETAG not in hdrs and "etag" not in hdrs:
            hdrs[HEADER_ETAG] = _file_etag(path, st.st_size, st.st_mtime)

        resp = Response.__new__(cls)
        Response.__init__(
            resp, status_code=HTTP_200_OK, body=body, content_type=content_type, headers=hdrs
        )
        return resp
