"""Response types — optimized serialization with orjson."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import orjson

# Reason-phrase lookup for `Response.status` — `HTTPStatus(code).phrase`
# walks the IntEnum on every access, which shows up on the hot
# status-line path. Build the mapping once at import time.
_STATUS_PHRASES: dict[int, str] = {s.value: s.phrase for s in HTTPStatus}


def _reject_header_crlf(value: str, what: str) -> str:
    """Reject CR, LF, or NUL in a header field name or value.

    Untrusted data carrying these characters enables HTTP response
    splitting / header injection. Raising — rather than silently
    stripping — surfaces the bug at the offending call site.
    """
    # Inline three `__contains__` calls — CPython short-circuits on the
    # first match, and a typical clean header value scans them in C.
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError(f"{what} contains an illegal control character (CR, LF, or NUL)")
    return value


def _format_content_disposition(disposition: str, filename: str) -> str:
    """Build a safe RFC 6266 ``Content-Disposition`` header value.

    The filename is reduced to a quoted ASCII ``filename="..."`` form with
    backslashes, double-quotes, and control characters neutralised, so a
    crafted filename cannot break out of the header. When the original
    name had non-ASCII characters, an RFC 5987 ``filename*=UTF-8''...``
    parameter is appended for modern browsers.
    """
    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    safe_ascii = "".join("_" if (c in '"\\' or c < " " or c == "\x7f") else c for c in ascii_name)
    value = f'{disposition}; filename="{safe_ascii}"'
    if ascii_name != filename:  # the original had non-ASCII characters
        value += f"; filename*=UTF-8''{quote(filename, safe='')}"
    return value


def _file_etag(path: str, size: int, mtime: float) -> str:
    """Strong, opaque-quoted ETag derived from (path, size, mtime).

    RFC 9110 §8.8.3 — the entity-tag is `quoted-string`. Using MD5 of
    the identity tuple keeps it deterministic across processes and
    matches the shape `StaticFiles._compute_etag` emits, so a static
    handler and a `FileResponse` over the same file validate against
    the same `If-None-Match` value.
    """
    key = f"{path}:{size}:{mtime}".encode()
    return f'"{hashlib.md5(key).hexdigest()}"'


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
        status_code: int = 200,
        body: bytes = b"",
        content_type: str = "text/plain",
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

    # ── `body` ───────────────────────────────────────────────────────
    # Backed by `_body` so the setter can invalidate the encode cache.
    # Middleware that mutates `response.body = new_bytes` after a prior
    # `.encode()` call would otherwise emit stale bytes + wrong
    # Content-Length on the next encode.

    @property
    def body(self) -> bytes:
        return self._body

    @body.setter
    def body(self, value: bytes) -> None:
        self._body = value
        self._encoded = None

    # ── `media_type` alias ────────────────────────────────────────────
    # ASGI servers name this attribute `media_type`; veloce's
    # canonical name is `content_type`. Expose both names so code that
    # uses either name reads and writes cleanly.

    @property
    def media_type(self) -> str:
        return self.content_type

    @media_type.setter
    def media_type(self, value: str) -> None:
        self.content_type = value
        # Invalidate any cached HTTP/1.1 encode so the new content type
        # takes effect on the next `encode()` call.
        self._encoded = None

    # ── `mimetype` ───────────────────────────────────────────────────
    # `mimetype` is the bare media type, with no parameters.
    # Setting it preserves the existing `charset` parameter.

    @property
    def is_json(self) -> bool:
        """True when `Content-Type` is JSON.

        Matches `application/json` and any `application/*+json`
        structured suffix (RFC 6839 §3.1).
        """
        mt = (self.content_type or "").split(";", 1)[0].strip().lower()
        if mt == "application/json":
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
        """The bare media type — `Content-Type` without parameters.

        `text/html; charset=utf-8` → `text/html`. Lower-cased and
        stripped per RFC 9110 §8.3 (media types are case-insensitive).
        """
        return (self.content_type or "").split(";", 1)[0].strip().lower()

    @mimetype.setter
    def mimetype(self, value: str) -> None:
        # Preserve the current charset parameter, if any.
        cs = self.charset
        ct = self.content_type or ""
        had_charset = "charset=" in ct
        self.content_type = f"{value}; charset={cs}" if had_charset else value
        self._encoded = None

    # ── `status` line ────────────────────────────────────────────────
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
        if isinstance(value, int):
            self.status_code = value
        else:
            # Take the leading integer token of "404 Not Found" / "404".
            head = value.strip().split(None, 1)[0]
            self.status_code = int(head)
        self._encoded = None

    def encode(self) -> bytes:
        """Encode to raw HTTP/1.1 bytes — called once, cached."""
        if self._encoded is not None:
            return self._encoded

        reason = _STATUS_PHRASES.get(self.status_code, "")
        parts = [f"HTTP/1.1 {self.status_code} {reason}".rstrip() + "\r\n"]

        user_headers = self.headers
        # User-supplied keys win for Content-Type/Length/Connection.
        # Detect case-insensitively so a user-passed "content-type"
        # doesn't get silently shadowed by — or duplicated alongside —
        # the framework default.
        user_keys_lc = {k.lower() for k in user_headers}
        if "content-type" not in user_keys_lc:
            parts.append(f"Content-Type: {self.content_type}\r\n")
        if "content-length" not in user_keys_lc:
            parts.append(f"Content-Length: {len(self.body)}\r\n")
        if "connection" not in user_keys_lc:
            parts.append("Connection: keep-alive\r\n")

        for key, value in user_headers.items():
            if key.lower() == "set-cookie":
                # One `Set-Cookie` dict entry may carry several cookies
                # joined by the internal separator; emit and CRLF-validate
                # each as its own header line.
                for line in str(value).split("\r\nSet-Cookie: "):
                    _reject_header_crlf(line, "Set-Cookie value")
                    parts.append(f"Set-Cookie: {line}\r\n")
            else:
                _reject_header_crlf(str(key), "header name")
                _reject_header_crlf(str(value), f"{key} header value")
                parts.append(f"{key}: {value}\r\n")
        parts.append("\r\n")

        self._encoded = "".join(parts).encode("latin-1") + self.body
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
    ) -> None:
        """Build a `Set-Cookie` header per RFC 6265.

        `samesite` defaults to `"Lax"` — a CSRF-resistant default that
        matches modern browser behaviour. Pass `samesite="None"` (with
        `secure=True`) for a cookie that must travel on cross-site
        requests, or `samesite=None`/`""` to omit the attribute.

        `expires=` accepts a `datetime`, a Unix timestamp `int|float`,
        or an already-formatted IMF-fixdate `str`. When both `max_age`
        and `expires` are set, both are emitted (RFC 6265 §5.2.2: clients
        prefer `Max-Age` when supported, falling back to `Expires` on
        legacy IE).

        `partitioned=True` adds the CHIPS `Partitioned` attribute
        (Cookies Having Independent Partitioned State) — a partitioned
        cookie is keyed to the top-level site, so embedded third-party
        contexts each get an isolated jar. `Partitioned` requires
        `Secure`, so it is only emitted when `secure=True`.

        The cookie name and value are rejected if they contain CR, LF, or
        NUL — untrusted data must not be able to inject additional cookies
        or response headers.
        """
        _reject_header_crlf(key, "cookie name")
        _reject_header_crlf(value, "cookie value")
        cookie = f"{key}={value}; Path={path}"
        if max_age is not None:
            # `max_age` may be passed as a `timedelta`; coerce to
            # whole seconds (the wire format is an integer).
            import datetime as _dt0

            if isinstance(max_age, _dt0.timedelta):
                max_age = int(max_age.total_seconds())
            cookie += f"; Max-Age={max_age}"
        if expires is not None:
            import datetime as _dt
            from email.utils import formatdate

            if isinstance(expires, _dt.datetime):
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=_dt.timezone.utc)
                stamp = expires.timestamp()
                cookie += f"; Expires={formatdate(stamp, usegmt=True)}"
            elif isinstance(expires, (int, float)):
                cookie += f"; Expires={formatdate(float(expires), usegmt=True)}"
            else:
                cookie += f"; Expires={expires}"
        if domain:
            cookie += f"; Domain={domain}"
        if secure:
            cookie += "; Secure"
        if httponly:
            cookie += "; HttpOnly"
        if samesite:
            cookie += f"; SameSite={samesite}"
        # CHIPS `Partitioned` — only valid alongside `Secure`.
        if partitioned and secure:
            cookie += "; Partitioned"
        existing = self.headers.get("Set-Cookie")
        if existing:
            self.headers["Set-Cookie"] = existing + "\r\nSet-Cookie: " + cookie
        else:
            self.headers["Set-Cookie"] = cookie
        self._encoded = None

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
        ct = self.content_type or "text/plain"
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
        self.headers["Content-Length"] = str(n)
        self._encoded = None
        return n

    @property
    def last_modified(self) -> Any:
        """Parsed `Last-Modified` header → UTC `datetime` or None.

        Accepts the three RFC 9110 §5.6.7 HTTP-date
        forms. Returns `None` on missing/unparseable.
        """
        raw = self.headers.get("Last-Modified") or self.headers.get("last-modified")
        if not raw:
            return None
        try:
            from email.utils import parsedate_to_datetime

            return parsedate_to_datetime(raw.strip())
        except (TypeError, ValueError):
            return None

    @last_modified.setter
    def last_modified(self, value: Any) -> None:
        self._set_http_date_header("Last-Modified", value)

    @property
    def expires(self) -> Any:
        """Parsed `Expires` header → UTC `datetime` or None (RFC 9111 §5.3)."""
        raw = self.headers.get("Expires") or self.headers.get("expires")
        if not raw:
            return None
        try:
            from email.utils import parsedate_to_datetime

            return parsedate_to_datetime(raw.strip())
        except (TypeError, ValueError):
            return None

    @expires.setter
    def expires(self, value: Any) -> None:
        self._set_http_date_header("Expires", value)

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
        import datetime as _dt
        from email.utils import formatdate

        if isinstance(value, _dt.datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=_dt.timezone.utc)
            self.headers[name] = formatdate(value.timestamp(), usegmt=True)
        elif isinstance(value, (int, float)):
            self.headers[name] = formatdate(float(value), usegmt=True)
        else:
            self.headers[name] = str(value)
        self._encoded = None

    @property
    def cookies(self) -> dict[str, str]:
        """Parsed cookie jar from this response's `Set-Cookie` header(s).

        Walks every `Set-Cookie` entry (Q44 separator `\\r\\nSet-Cookie: `
        respected) and returns `{name: value}`. Multiple cookies with
        the same name resolve to the last set — matches the wire
        behaviour where the client also keeps the most-recent value.
        Caller introspection only; mutation goes through `set_cookie()`.
        """
        out: dict[str, str] = {}
        existing = self.headers.get("Set-Cookie", "") or self.headers.get("set-cookie", "")
        if not existing:
            return out
        # Q44 emits multi-cookies as `cookie1\r\nSet-Cookie: cookie2…`.
        for line in existing.split("\r\nSet-Cookie:"):
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
                for piece in v.split("\r\nSet-Cookie:"):
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
        for key in ("Content-Length", "content-length"):
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
        """Build and set the `Cache-Control` header — RFC 9111 §5.2.

        Combines the standard directives in the order RFC 9111 §5.2
        documents. Values that are False / None are omitted, so a plain
        `resp.set_cache_control(max_age=3600, public=True)` produces
        `Cache-Control: public, max-age=3600`. Returns the value set.
        """
        parts: list[str] = []
        if public:
            parts.append("public")
        if private:
            parts.append("private")
        if no_cache:
            parts.append("no-cache")
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
            self.headers["Cache-Control"] = value
            self._encoded = None
        return value

    def add_vary(self, *header_names: str) -> str:
        """Append header names to the `Vary` response header — RFC 9110 §12.5.5.

        Merges with any existing `Vary` value (de-duplicates,
        case-insensitive). Returns the resulting header value.
        Useful when middleware wants to communicate "this response
        depends on the named request headers" without clobbering
        existing entries.
        """
        existing = self.headers.get("Vary", "") or self.headers.get("vary", "")
        existing_set = {p.strip().lower() for p in existing.split(",") if p.strip()}
        result: list[str] = [p.strip() for p in existing.split(",") if p.strip()]
        for name in header_names:
            if name.lower() not in existing_set:
                result.append(name)
                existing_set.add(name.lower())
        value = ", ".join(result)
        # Always write under `Vary` (canonical case) and clear any
        # lower-case duplicate.
        self.headers.pop("vary", None)
        self.headers["Vary"] = value
        self._encoded = None
        return value

    @property
    def vary(self) -> Any:
        """The `Vary` header as a `HeaderSet`.

        Returns a fresh `HeaderSet` parsed from the current header.
        Assign a `HeaderSet`, iterable of strings, or a comma-separated
        string to replace it. Mutating the returned object does *not*
        write back — call `add_vary(...)` or reassign for that.
        """
        from veloce.http.header_set import HeaderSet

        return HeaderSet(self.headers.get("Vary", ""))

    @vary.setter
    def vary(self, value: Any) -> None:
        from veloce.http.header_set import HeaderSet

        hs = value if isinstance(value, HeaderSet) else HeaderSet(value)
        self.headers.pop("vary", None)
        self.headers["Vary"] = hs.to_header()
        self._encoded = None

    @property
    def allow(self) -> Any:
        """The `Allow` header as a `HeaderSet`.

        Lists the HTTP methods the resource supports (RFC 9110 §10.2.1).
        Assign a `HeaderSet`, iterable, or comma-separated string.
        """
        from veloce.http.header_set import HeaderSet

        return HeaderSet(self.headers.get("Allow", ""))

    @allow.setter
    def allow(self, value: Any) -> None:
        from veloce.http.header_set import HeaderSet

        hs = value if isinstance(value, HeaderSet) else HeaderSet(value)
        self.headers.pop("allow", None)
        self.headers["Allow"] = hs.to_header()
        self._encoded = None

    @property
    def www_authenticate(self) -> str | None:
        """The `WWW-Authenticate` challenge header — RFC 9110 §11.6.1.

        Sent on `401 Unauthorized` to tell the client which auth
        scheme(s) to use. `None` when unset.
        """
        return self.headers.get("WWW-Authenticate")

    @www_authenticate.setter
    def www_authenticate(self, value: str | None) -> None:
        if value is None:
            self.headers.pop("WWW-Authenticate", None)
        else:
            self.headers["WWW-Authenticate"] = value
        self._encoded = None

    def set_basic_auth_challenge(self, realm: str = "Authentication Required") -> str:
        """Write a `Basic` `WWW-Authenticate` challenge — RFC 7617.

        Convenience for the common 401 case:
        `WWW-Authenticate: Basic realm="<realm>", charset="UTF-8"`.
        Returns the header value written.
        """
        value = f'Basic realm="{realm}", charset="UTF-8"'
        self.headers["WWW-Authenticate"] = value
        self._encoded = None
        return value

    @property
    def content_encoding(self) -> str | None:
        """The `Content-Encoding` header — RFC 9110 §8.4. `None` when unset."""
        return self.headers.get("Content-Encoding")

    @content_encoding.setter
    def content_encoding(self, value: str | None) -> None:
        if value is None:
            self.headers.pop("Content-Encoding", None)
        else:
            self.headers["Content-Encoding"] = value
        self._encoded = None

    @property
    def content_language(self) -> str | None:
        """The `Content-Language` header — RFC 9110 §8.5. `None` when unset."""
        return self.headers.get("Content-Language")

    @content_language.setter
    def content_language(self, value: str | None) -> None:
        if value is None:
            self.headers.pop("Content-Language", None)
        else:
            self.headers["Content-Language"] = value
        self._encoded = None

    @property
    def accept_ranges(self) -> str | None:
        """The `Accept-Ranges` header — RFC 9110 §14.3.

        Typically `bytes` (range requests supported) or `none`
        (explicitly unsupported). `None` when the header is unset.
        """
        return self.headers.get("Accept-Ranges")

    @accept_ranges.setter
    def accept_ranges(self, value: str | None) -> None:
        if value is None:
            self.headers.pop("Accept-Ranges", None)
        else:
            self.headers["Accept-Ranges"] = value
        self._encoded = None

    def set_content_range(
        self, start: int | None, stop: int | None, length: int | None, unit: str = "bytes"
    ) -> str:
        """Write a `Content-Range` header — RFC 9110 §14.4.

        - `set_content_range(0, 499, 1234)` → `bytes 0-499/1234`.
        - `start`/`stop` both `None` → an unsatisfied-range response:
          `bytes */1234` (length required in that form).
        - `length` `None` → unknown total: `bytes 0-499/*`.

        Returns the header value written.
        """
        if start is None or stop is None:
            total = "*" if length is None else str(length)
            value = f"{unit} */{total}"
        else:
            total = "*" if length is None else str(length)
            value = f"{unit} {start}-{stop}/{total}"
        self.headers["Content-Range"] = value
        self._encoded = None
        return value

    @property
    def content_range(self) -> str | None:
        """The raw `Content-Range` header — RFC 9110 §14.4. `None` if unset."""
        return self.headers.get("Content-Range")

    @property
    def date(self) -> Any:
        """The `Date` header as a tz-aware UTC `datetime` — RFC 9110 §6.6.1.

        Returns `None` when unset or unparseable. Assign a `datetime`
        or POSIX timestamp to set it; assign `None` to remove it.
        """
        from veloce.http.dates import parse_date

        return parse_date(self.headers.get("Date"))

    @date.setter
    def date(self, value: Any) -> None:
        if value is None:
            self.headers.pop("Date", None)
        else:
            from veloce.http.dates import http_date

            self.headers["Date"] = http_date(value)
        self._encoded = None

    @property
    def location(self) -> str | None:
        """The `Location` header — RFC 9110 §10.2.2. `None` when unset."""
        return self.headers.get("Location")

    @location.setter
    def location(self, value: str | None) -> None:
        if value is None:
            self.headers.pop("Location", None)
        else:
            self.headers["Location"] = value
        self._encoded = None

    @property
    def content_location(self) -> str | None:
        """The `Content-Location` header — RFC 9110 §8.7. `None` when unset."""
        return self.headers.get("Content-Location")

    @content_location.setter
    def content_location(self, value: str | None) -> None:
        if value is None:
            self.headers.pop("Content-Location", None)
        else:
            self.headers["Content-Location"] = value
        self._encoded = None

    @property
    def retry_after(self) -> Any:
        """The `Retry-After` header — RFC 9110 §10.2.3.

        Returns an `int` (delay in seconds) when the header is numeric,
        a tz-aware `datetime` when it's an HTTP-date, or `None` when
        unset. Assign an int / `timedelta` / `datetime` to set it;
        assign `None` to remove it.
        """
        raw = self.headers.get("Retry-After")
        if not raw:
            return None
        raw = raw.strip()
        if raw.isdigit():
            return int(raw)
        from veloce.http.dates import parse_date

        return parse_date(raw)

    @retry_after.setter
    def retry_after(self, value: Any) -> None:
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        if value is None:
            self.headers.pop("Retry-After", None)
        elif isinstance(value, _td):
            self.headers["Retry-After"] = str(int(value.total_seconds()))
        elif isinstance(value, _dt):
            from veloce.http.dates import http_date

            self.headers["Retry-After"] = http_date(value)
        else:
            self.headers["Retry-After"] = str(int(value))
        self._encoded = None

    @property
    def age(self) -> int | None:
        """The `Age` header in seconds — RFC 9110 §5.1. `None` when unset."""
        raw = self.headers.get("Age")
        if not raw or not raw.strip().isdigit():
            return None
        return int(raw.strip())

    @age.setter
    def age(self, value: int | None) -> None:
        if value is None:
            self.headers.pop("Age", None)
        else:
            self.headers["Age"] = str(int(value))
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
        self.headers["ETag"] = etag
        self._encoded = None

    def get_etag(self) -> tuple[str | None, bool]:
        """Return `(etag, is_weak)` parsed from the `ETag` header.

        `(None, False)` when unset. Returned tag keeps its quotes so
        it compares directly with `If-None-Match` values.
        """
        raw = self.headers.get("ETag") or self.headers.get("etag")
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
        from veloce.http.cache_control import CacheControl

        return CacheControl(self.headers.get("Cache-Control", ""))

    def iter_encoded(self) -> Any:
        """Yield the response body.

        Buffered → single-chunk iter over `body`. Streaming → proxy
        to the underlying async iterator. Lets callers drain a
        response without going through ASGI emit.
        """
        stream = self._stream
        if stream is not None:
            return stream
        return iter([self.body]) if self.body else iter([])

    def iter_chunked(self, size: int) -> Any:
        """Yield the response body in fixed-size chunks.

        Buffered responses are split into `size`-byte slices (final
        slice may be shorter). Streaming responses are returned
        unchanged — the chunk boundaries are then controlled by the
        underlying generator, not the caller. `size` must be positive.
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

        Uses MD5 of the response body, opaque-quoted per RFC 9110 §8.8.3.
        `weak=True` prepends `W/` so the validator is treated as a
        weak match (matching content but possibly different
        byte-for-byte). Sets `ETag` even if one was already set; pass
        the explicit ETag in `__init__(headers=...)` to skip this.
        Returns the value set.
        """
        import hashlib

        digest = hashlib.md5(self.body).hexdigest()
        etag = f'"{digest}"' if not weak else f'W/"{digest}"'
        self.headers["ETag"] = etag
        self._encoded = None
        return etag

    def make_conditional(self, request: Any) -> Response:
        """Downgrade this response to 304 when the request's preconditions
        match the response's ETag / Last-Modified.

        Checks `If-None-Match` first (per RFC 9110 §13.2 precedence),
        then `If-Modified-Since`. On a match, mutates `self` to status
        304 with no body. Returns `self` so callers can use it inline:
        `return resp.make_conditional(request)`.

        Handles `If-None-Match: *` (matches any current representation
        of the resource) and the weak/strong ETag comparison rules.
        """
        # If-None-Match: any token (or `*`) that equals the response's
        # ETag returns 304.
        ours_etag = self.headers.get("ETag", "")
        inm = getattr(request, "if_none_match", ())
        if inm and ours_etag:
            if "*" in inm:
                self._downgrade_to_304()
                return self
            # Strong comparison: strip `W/` prefixes from both sides.
            ours_stripped = ours_etag.removeprefix("W/")
            for tag in inm:
                if tag.removeprefix("W/") == ours_stripped:
                    self._downgrade_to_304()
                    return self
            # Explicit non-match — caller's other preconditions don't apply.
            return self

        # If-Modified-Since (only consulted when If-None-Match absent).
        ours_lm = self.headers.get("Last-Modified", "")
        ims = getattr(request, "if_modified_since", None)
        if ims is not None and ours_lm:
            from email.utils import parsedate_to_datetime

            try:
                ours_ts = parsedate_to_datetime(ours_lm).timestamp()
            except (TypeError, ValueError):
                return self
            # HTTP-date second resolution — integer floor.
            if int(ours_ts) <= int(ims):
                self._downgrade_to_304()
        return self

    def _downgrade_to_304(self) -> None:
        """Strip body + flip status to 304. Used by `make_conditional`."""
        self.status_code = 304
        self.body = b""
        # `Content-Length` will be recomputed on encode/emit; explicit
        # `Content-Type` removal so a 304 doesn't advertise a media type
        # for a body it isn't sending (RFC 9110 §15.4.5).
        self.headers.pop("Content-Length", None)
        self.headers.pop("content-length", None)
        self._encoded = None

    def set_content_disposition(
        self, disposition: str = "attachment", filename: str | None = None
    ) -> str:
        """Write a `Content-Disposition` header — RFC 6266.

        `disposition` is `"attachment"` (force download) or `"inline"`
        (render in-browser). When `filename` is given it is added as
        the `filename` parameter; non-ASCII names also get the
        RFC 5987 `filename*=UTF-8''…` form for modern browsers.
        Returns the header value written.
        """
        value = _format_content_disposition(disposition, filename) if filename else disposition
        self.headers["Content-Disposition"] = value
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
    ) -> None:
        """Delete a cookie by overwriting it with an empty value + Max-Age=0.

        The browser only treats the new cookie as a replacement for the
        existing one if `Path`, `Domain`, **and the `Secure` / `SameSite`
        attributes match** — otherwise it stores both. So a session
        cookie originally set with `Secure; SameSite=None` will not be
        deleted by a plain `delete_cookie(key)` call. Pass the same
        flags here.
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
        )


class JSONResponse(Response):
    """JSON response using orjson for speed."""

    def __init__(
        self,
        data: Any,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = orjson.dumps(data)
        super().__init__(
            status_code=status_code,
            body=body,
            content_type="application/json",
            headers=headers,
        )


class ORJSONResponse(JSONResponse):
    """Explicit orjson-backed JSON response.

    `JSONResponse` already uses `orjson` for encoding, so this class is a
    semantic alias — useful when route declarations want to communicate
    the encoder choice via `response_class=ORJSONResponse`.
    """


class UJSONResponse(Response):
    """JSON response encoded with `ujson`.

    Lazily imports `ujson` at construction. Raises `ImportError` with a
    clear message when the package is missing rather than at module load,
    so apps that don't use this class don't need ujson installed.
    """

    def __init__(
        self,
        data: Any,
        status_code: int = 200,
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
            content_type="application/json",
            headers=headers,
        )


class HTMLResponse(Response):
    """HTML response."""

    def __init__(
        self,
        content: str,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            body=content.encode("utf-8"),
            content_type="text/html; charset=utf-8",
            headers=headers,
        )


class PlainTextResponse(Response):
    """Plain text response."""

    def __init__(
        self,
        content: str,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            body=content.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
            headers=headers,
        )


class RedirectResponse(Response):
    """HTTP redirect."""

    def __init__(
        self,
        url: str,
        status_code: int = 307,
        headers: dict[str, str] | None = None,
    ) -> None:
        hdrs = headers or {}
        # Reject CR/LF in the target and percent-encode it, so a crafted
        # URL or Host header cannot inject extra response headers. The
        # safe set keeps URL-structural characters (RFC 3986) and `%`
        # so an already-encoded URL is not double-encoded.
        _reject_header_crlf(url, "redirect URL")
        hdrs["Location"] = quote(url, safe="/:?#[]@!$&'()*+,;=%~")
        super().__init__(
            status_code=status_code,
            body=b"",
            content_type="text/plain",
            headers=hdrs,
        )


class StreamingResponse(Response):
    """Streaming response for large payloads.

    `content` may be an async iterator/iterable **or** a plain sync
    iterable (e.g. a generator). A sync iterable is wrapped so the
    response always exposes an async stream; both forms are accepted.
    """

    def __init__(
        self,
        content: Any,
        status_code: int = 200,
        content_type: str = "application/octet-stream",
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
        reason = HTTPStatus(self.status_code).phrase
        parts = [f"HTTP/1.1 {self.status_code} {reason}\r\n"]
        final_headers = {
            "Content-Type": self.content_type,
            "Transfer-Encoding": "chunked",
            "Connection": "keep-alive",
        }
        final_headers.update(self.headers)
        for key, value in final_headers.items():
            if key.lower() == "set-cookie":
                for line in str(value).split("\r\nSet-Cookie: "):
                    _reject_header_crlf(line, "Set-Cookie value")
                    parts.append(f"Set-Cookie: {line}\r\n")
            else:
                _reject_header_crlf(str(key), "header name")
                _reject_header_crlf(str(value), f"{key} header value")
                parts.append(f"{key}: {value}\r\n")
        parts.append("\r\n")
        return "".join(parts).encode("latin-1")

    async def stream_to(self, transport: Any) -> None:
        """Stream chunks to transport."""
        transport.write(self.encode())
        async for chunk in self._stream:
            size = format(len(chunk), "x")
            transport.write(f"{size}\r\n".encode() + chunk + b"\r\n")
        transport.write(b"0\r\n\r\n")


class FileResponse(Response):
    """Serve a file from disk — uses async I/O via executor."""

    def __init__(
        self,
        path: str,
        filename: str | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
        content_disposition_type: str = "attachment",
    ) -> None:
        # Validate path exists (cheap stat check — actual read is deferred)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"File not found: {path}")

        self._file_path = path

        if content_type is None:
            content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"

        hdrs = headers or {}
        if filename:
            # `content_disposition_type` — "attachment" (force a
            # download dialog) or "inline" (render in the browser).
            hdrs["Content-Disposition"] = _format_content_disposition(
                content_disposition_type, filename
            )

        st = os.stat(path)
        if "Last-Modified" not in hdrs and "last-modified" not in hdrs:
            from email.utils import formatdate

            hdrs["Last-Modified"] = formatdate(st.st_mtime, usegmt=True)
        if "ETag" not in hdrs and "etag" not in hdrs:
            hdrs["ETag"] = _file_etag(path, st.st_size, st.st_mtime)

        # Warn when called on a running loop — a 50 MB read on the loop
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
            status_code=200,
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
        content_disposition_type: str = "attachment",
    ) -> FileResponse:
        """Async factory — reads file in executor to avoid blocking event loop."""
        loop = asyncio.get_running_loop()

        def _read_file() -> bytes:
            if not os.path.isfile(path):
                raise FileNotFoundError(f"File not found: {path}")
            with open(path, "rb") as f:
                return f.read()

        body = await loop.run_in_executor(None, _read_file)
        st = await loop.run_in_executor(None, os.stat, path)

        if content_type is None:
            content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"

        hdrs = headers or {}
        if filename:
            hdrs["Content-Disposition"] = _format_content_disposition(
                content_disposition_type, filename
            )
        if "Last-Modified" not in hdrs and "last-modified" not in hdrs:
            from email.utils import formatdate

            hdrs["Last-Modified"] = formatdate(st.st_mtime, usegmt=True)
        if "ETag" not in hdrs and "etag" not in hdrs:
            hdrs["ETag"] = _file_etag(path, st.st_size, st.st_mtime)

        resp = Response.__new__(cls)
        Response.__init__(resp, status_code=200, body=body, content_type=content_type, headers=hdrs)
        resp._file_path = path
        return resp
