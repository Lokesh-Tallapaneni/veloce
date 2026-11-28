"""Request negotiation — Accept, authorization, CORS, conditional requests and cookies."""

from __future__ import annotations

from veloce._constants import (
    HEADER_ACCEPT,
    HEADER_ACCEPT_CHARSET,
    HEADER_ACCEPT_ENCODING,
    HEADER_ACCEPT_LANGUAGE,
    HEADER_ACCESS_CONTROL_REQUEST_HEADERS,
    HEADER_ACCESS_CONTROL_REQUEST_METHOD,
    HEADER_AUTHORIZATION,
    HEADER_CACHE_CONTROL,
    HEADER_COOKIE,
    HEADER_IF_MATCH,
    HEADER_IF_MODIFIED_SINCE,
    HEADER_IF_NONE_MATCH,
    HEADER_IF_RANGE,
    HEADER_IF_UNMODIFIED_SINCE,
    HEADER_RANGE,
)
from veloce.http.cache_control import CacheControl
from veloce.http.datastructures import (
    AcceptHeader,
    Authorization,
    Cookies,
    RangeSpec,
)
from veloce.http.dates import parse_date
from veloce.http.request._base import (
    _UNSET,
    RequestBase,
)

# Pre-encoded for `_peek_header_key`, which reads one header without the mapping.
_RAW_ACCEPT = HEADER_ACCEPT.lower().encode("latin-1")

_RAW_ACCEPT_LANGUAGE = HEADER_ACCEPT_LANGUAGE.lower().encode("latin-1")

_RAW_ACCEPT_ENCODING = HEADER_ACCEPT_ENCODING.lower().encode("latin-1")

_RAW_ACCEPT_CHARSET = HEADER_ACCEPT_CHARSET.lower().encode("latin-1")


def _split_etag_list(value: str) -> tuple[str, ...]:
    """Split an `If-Match`/`If-None-Match` list on commas outside quoted strings.

    RFC 9110 Sec. 8.8.3 `etagc = %x21 / %x23-7E / obs-text` permits a comma inside
    an opaque-tag's quoted string, so a naive `split(",")` corrupts a valid tag
    like `"abc,def"`. Track whether the scan is inside double quotes and only
    break on a comma seen at the top level. The `W/` weak prefix and the
    surrounding quotes are preserved verbatim for caller comparison.
    """
    tags: list[str] = []
    start = 0
    in_quotes = False
    for i, ch in enumerate(value):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            tag = value[start:i].strip()
            if tag:
                tags.append(tag)
            start = i + 1
    tail = value[start:].strip()
    if tail:
        tags.append(tail)
    return tuple(tags)


class NegotiationMixin(RequestBase):
    """Content negotiation, validators and cookies."""

    __slots__ = ()

    # ── Content negotiation (Accept) ──────────────────────
    @property
    def accept(self) -> str:
        """Return the raw Accept header value."""
        return self._peek_header_key(_RAW_ACCEPT) or ""

    @property
    def accept_mimetypes(self) -> AcceptHeader:
        """Parsed `Accept` header with MIME wildcard matching."""
        if self._accept_mimetypes is None:
            self._accept_mimetypes = AcceptHeader.parse(
                self._peek_header_key(_RAW_ACCEPT) or "", mime=True
            )
        return self._accept_mimetypes

    @property
    def accept_languages(self) -> AcceptHeader:
        """Parsed `Accept-Language` header. q-value ordered."""
        if self._accept_languages is None:
            self._accept_languages = AcceptHeader.parse(
                self._peek_header_key(_RAW_ACCEPT_LANGUAGE) or ""
            )
        return self._accept_languages

    @property
    def accept_encodings(self) -> AcceptHeader:
        """Parsed `Accept-Encoding` header (e.g. gzip, br)."""
        if self._accept_encodings is None:
            self._accept_encodings = AcceptHeader.parse(
                self._peek_header_key(_RAW_ACCEPT_ENCODING) or ""
            )
        return self._accept_encodings

    @property
    def accept_charsets(self) -> AcceptHeader:
        """Parsed `Accept-Charset` header."""
        if self._accept_charsets is None:
            self._accept_charsets = AcceptHeader.parse(
                self._peek_header_key(_RAW_ACCEPT_CHARSET) or ""
            )
        return self._accept_charsets

    # ── Authorization ─────────────────────────────────────
    @property
    def authorization(self) -> str | None:
        """Return the parsed Authorization header."""
        return self.headers.get(HEADER_AUTHORIZATION)

    @property
    def auth(self) -> Authorization | None:
        """Lazy-parse the `Authorization:` header into a typed object.

        Returns `None` when the header is missing. `Basic` and `Bearer`
        schemes populate `.username/.password` and `.token` respectively;
        other schemes carry their key=value parameters in `.params`.
        """
        cached = self._auth
        if cached is _UNSET:
            cached = Authorization.from_header(self.headers.get(HEADER_AUTHORIZATION, ""))
            self._auth = cached
        return cached

    # ── CORS preflight (RFC 6454) ─────────────────────────
    @property
    def access_control_request_method(self) -> str | None:
        """CORS preflight `Access-Control-Request-Method` - RFC 6454.

        On an `OPTIONS` preflight, names the method the real request
        will use. `None` outside a preflight.
        """
        return self.headers.get(HEADER_ACCESS_CONTROL_REQUEST_METHOD)

    @property
    def access_control_request_headers(self) -> list[str]:
        """CORS preflight `Access-Control-Request-Headers` - header list.

        The headers the real request intends to send, lower-cased and
        whitespace-trimmed. Empty list when the header is absent.
        """
        cached = self._access_control_request_headers
        if cached is None:
            raw = self.headers.get(HEADER_ACCESS_CONTROL_REQUEST_HEADERS, "")
            cached = [h.strip().lower() for h in raw.split(",") if h.strip()]
            self._access_control_request_headers = cached
        return cached

    # ── Conditional requests (RFC 9110 Sec. 13) ───────────
    @property
    def if_modified_since(self) -> float | None:
        """Parse `If-Modified-Since` (RFC 9110 Sec. 13.1.3) to a Unix timestamp.

        Accepts IMF-fixdate, obsolete RFC 850, and ANSI C `asctime()`
        forms. Returns `None` when the header is missing or unparseable
        - never raises, so callers can use it in a single branch.
        """
        cached = self._if_modified_since
        if cached is _UNSET:
            raw = self.headers.get(HEADER_IF_MODIFIED_SINCE)
            if not raw:
                cached = None
            else:
                dt = parse_date(raw)
                cached = dt.timestamp() if dt else None
            self._if_modified_since = cached
        return cached

    @property
    def if_unmodified_since(self) -> float | None:
        """Parse `If-Unmodified-Since` (RFC 9110 Sec. 13.1.4) to a Unix timestamp.

        Returns `None` when the header is missing or unparseable.
        Write-side companion to `If-Modified-Since`: precondition that
        fails with `412` when the resource has been modified since the
        given date.
        """
        cached = self._if_unmodified_since
        if cached is _UNSET:
            raw = self.headers.get(HEADER_IF_UNMODIFIED_SINCE)
            if not raw:
                cached = None
            else:
                dt = parse_date(raw)
                cached = dt.timestamp() if dt else None
            self._if_unmodified_since = cached
        return cached

    @property
    def if_match(self) -> tuple[str, ...]:
        """Parse `If-Match` (RFC 9110 Sec. 13.1.1) into a tuple of ETags.

        Returns `("*",)` for the wildcard, an empty tuple when the
        header is absent, otherwise a tuple of quoted ETags (quotes
        and any `W/` weak prefix preserved verbatim).

        `If-Match` is the write-side companion to `If-None-Match`:
        precondition that fails the request with `412 Precondition
        Failed` when none of the listed ETags matches the resource's
        current ETag. Standard guard against the lost-update problem.
        """
        cached = self._if_match
        if cached is None:
            raw = self.headers.get(HEADER_IF_MATCH, "")
            if not raw:
                cached = ()
            else:
                stripped = raw.strip()
                cached = ("*",) if stripped == "*" else _split_etag_list(stripped)
            self._if_match = cached
        return cached

    @property
    def if_none_match(self) -> tuple[str, ...]:
        """Parse `If-None-Match` (RFC 9110 Sec. 13.1.4) into a tuple of ETags.

        Returns `("*",)` when the header is the literal `*` (matches any
        existing representation), an empty tuple when the header is
        missing, or a tuple of one or more quoted ETags (the quotes are
        preserved so callers can compare them verbatim against an ETag
        header on the response).
        """
        cached = self._if_none_match
        if cached is None:
            raw = self.headers.get(HEADER_IF_NONE_MATCH, "")
            if not raw:
                cached = ()
            else:
                stripped = raw.strip()
                # Keeps `W/` and does not break on a comma inside a quoted tag.
                cached = ("*",) if stripped == "*" else _split_etag_list(stripped)
            self._if_none_match = cached
        return cached

    @property
    def if_range(self) -> tuple[str, float | None]:
        """Parse `If-Range:` (RFC 9110 Sec. 13.1.5).

        The header carries **either** an ETag **or** an HTTP-date - never
        both. Returns `(etag, None)` when the value is an ETag (quoted,
        possibly weak-prefixed) and `("", timestamp)` when it parses as
        a date. Returns `("", None)` when the header is absent or
        unparseable. Caller picks the relevant slot.

        Used by `GET` with `Range:` to convert a partial-content request
        into a full `200` when the cached resource is stale.
        """
        cached = self._if_range
        if cached is None:
            raw = self.headers.get(HEADER_IF_RANGE, "")
            if not raw:
                cached = ("", None)
            else:
                stripped = raw.strip()
                # An ETag is quoted, optionally `W/`; anything else is a date.
                if stripped.startswith('"') or stripped.startswith('W/"'):
                    cached = (stripped, None)
                else:
                    dt = parse_date(stripped)
                    cached = ("", None) if dt is None else ("", dt.timestamp())
            self._if_range = cached
        return cached

    @property
    def range(self) -> RangeSpec | None:
        """Parse the `Range:` header per RFC 9110 Sec. 14.2.

        `None` when the header is absent or unparseable.
        """
        cached = self._range
        if cached is _UNSET:
            cached = RangeSpec.parse(self.headers.get(HEADER_RANGE, ""))
            self._range = cached
        return cached

    # ── Caching directives ────────────────────────────────
    @property
    def cache_control(self) -> CacheControl:
        """Parsed `Cache-Control` header.

        Returns a `CacheControl` view: `req.cache_control.no_cache`
        (bool), `req.cache_control.max_age` (int or None), etc.
        Always returns a fresh parse to reflect any header mutation.
        """
        return CacheControl(self.headers.get(HEADER_CACHE_CONTROL, ""))

    # ── Cookies ───────────────────────────────────────────
    @property
    def cookies(self) -> Cookies:
        """Parse cookies from the `Cookie` header - lazy, MultiDict-shaped.

        Returns a `Cookies` (MultiDict). `cookies["name"]` gives the first
        value; `cookies.getlist("name")` gives every value when a name
        repeats (rare but valid per RFC 6265).
        """
        if self._cookies is None:
            # RFC 6265 permits several `Cookie` headers, merged with `; ` here.
            parts = self.headers.getlist(HEADER_COOKIE)
            if not parts:
                header = ""
            elif len(parts) == 1:
                header = parts[0]
            else:
                header = "; ".join(parts)
            self._cookies = Cookies.from_cookie_header(header)
        return self._cookies
