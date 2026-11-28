"""Response headers — cookies, dates, caching, negotiation and single-value accessors."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from veloce._constants import (
    HEADER_ACCEPT_RANGES,
    HEADER_AGE,
    HEADER_ALLOW,
    HEADER_CACHE_CONTROL,
    HEADER_CONTENT_ENCODING,
    HEADER_CONTENT_LANGUAGE,
    HEADER_CONTENT_LOCATION,
    HEADER_CONTENT_RANGE,
    HEADER_DATE,
    HEADER_EXPIRES,
    HEADER_LAST_MODIFIED,
    HEADER_LOCATION,
    HEADER_RETRY_AFTER,
    HEADER_SET_COOKIE,
    HEADER_VALUE_BYTES,
    HEADER_VALUE_NO_CACHE,
    HEADER_VALUE_PUBLIC,
    HEADER_VARY,
    HEADER_WWW_AUTHENTICATE,
)
from veloce._internal import (
    _reject_header_crlf,
)
from veloce._protocol_constants import (
    AUTH_SCHEME_BASIC,
    SET_COOKIE_JOINER,
)
from veloce.enums import CookiePrefix, SameSite
from veloce.http.cookies import dump_cookie, iter_cookies
from veloce.http.dates import http_date, parse_date
from veloce.http.header_set import HeaderSet

# ── Case-insensitive response-header access ──────────────────────────
# `Response.headers` is case-SENSITIVE but HTTP field names are not (RFC 9110
# Sec. 5.1), so these fold the lookup without allocating a dict per call.
from veloce.http.response._base import ResponseBase
from veloce.http.response._headers import (
    _MAX_VARY_MERGES,
    _VARY_MERGES,
    header_get,
    header_key,
    header_pop,
)


class HeadersMixin(ResponseBase):
    """Typed accessors over the response's header mapping."""

    __slots__ = ()

    def set_cookie(
        self,
        key: str,
        value: str,
        max_age: int | timedelta | None = None,
        expires: int | float | datetime | date | str | None = None,
        path: str = "/",
        domain: str | None = None,
        *,
        secure: bool = False,
        httponly: bool = False,
        samesite: SameSite | str | None = "Lax",
        partitioned: bool = False,
        prefix: CookiePrefix | Literal["host", "secure"] | None = None,
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

    def set_cache_control(
        self,
        max_age: int | None = None,
        *,
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
