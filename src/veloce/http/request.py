"""Request object — lazy parsing for speed."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

import orjson
from multidict import MultiDict

from veloce._constants import (
    HEADER_ACCEPT,
    HEADER_ACCEPT_CHARSET,
    HEADER_ACCEPT_ENCODING,
    HEADER_ACCEPT_LANGUAGE,
    HEADER_ACCESS_CONTROL_REQUEST_HEADERS,
    HEADER_ACCESS_CONTROL_REQUEST_METHOD,
    HEADER_AUTHORIZATION,
    HEADER_CACHE_CONTROL,
    HEADER_CONTENT_ENCODING,
    HEADER_CONTENT_LANGUAGE,
    HEADER_CONTENT_LENGTH,
    HEADER_CONTENT_TYPE,
    HEADER_COOKIE,
    HEADER_DATE,
    HEADER_HOST,
    HEADER_IF_MATCH,
    HEADER_IF_MODIFIED_SINCE,
    HEADER_IF_NONE_MATCH,
    HEADER_IF_RANGE,
    HEADER_IF_UNMODIFIED_SINCE,
    HEADER_MAX_FORWARDS,
    HEADER_ORIGIN,
    HEADER_PRAGMA,
    HEADER_RANGE,
    HEADER_REFERER,
    HEADER_USER_AGENT,
    HEADER_X_FORWARDED_FOR,
    HEADER_X_REQUESTED_WITH,
    MIME_FORM_URLENCODED,
    MIME_MULTIPART_FORM_DATA,
)
from veloce._header_parsing import parse_media_type_params
from veloce._internal import _coerce_bool, is_json_mimetype
from veloce._protocol_constants import URL_SCHEME_HTTPS
from veloce.exceptions import BadRequest, RequestEntityTooLarge
from veloce.http.cache_control import CacheControl
from veloce.http.datastructures import (
    DEFAULT_MAX_MULTIPART_PART_SIZE,
    DEFAULT_MAX_MULTIPART_PARTS,
    URL,
    AcceptHeader,
    Address,
    Authorization,
    Cookies,
    FormData,
    Headers,
    QueryParams,
    RangeSpec,
    State,
    UploadFile,
    _parse_qs_pairs,
    parse_multipart_form,
)
from veloce.http.dates import parse_date

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

_logger = logging.getLogger(__name__)

# Sentinel for "this conditional-header property has not been read yet"
# on properties whose legitimate parsed value can be `None` (`if_modified_since`,
# `if_unmodified_since`). Distinct from `None` so we can tell "absent
# header" apart from "haven't looked yet".
_UNSET: Any = object()

# Sentinel for "the JSON body has not been parsed yet" on the `_json` cache.
# Distinct from `None` so a body that is legitimately JSON `null` caches as
# `None` instead of looking unparsed and being re-decoded on every access.
_UNPARSED: Any = object()


def _split_etag_list(value: str) -> tuple[str, ...]:
    """Split an `If-Match`/`If-None-Match` list on commas outside quoted strings.

    RFC 9110 §8.8.3 `etagc = %x21 / %x23-7E / obs-text` permits a comma inside
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


class Request:
    """Incoming HTTP request with lazy attribute parsing.

    All expensive operations (JSON parsing, cookie parsing, URL construction,
    form/multipart parsing) are deferred until accessed — zero overhead for
    properties you don't use.

    Usage::

        @app.get("/users/{user_id}")
        async def show(request: Request, user_id: str):
            data = await request.json()
            agent = request.headers.get("user-agent", "")
            return {"id": user_id, "agent": agent, "body": data}
    """

    __slots__ = (
        "method",
        "path",
        "query_string",
        "_headers",
        "_headers_raw",
        "_body",
        "_body_drained",
        "_length_enforced",
        "_body_source",
        "transport",
        "app",
        "scope",
        "endpoint",
        "_json",
        "_query_params",
        "_form",
        "_path_params",
        "_state",
        "_cookies",
        "_url",
        "_background_tasks",
        "_parsed_ct",
        "_accept_mimetypes",
        "_accept_languages",
        "_accept_encodings",
        "_accept_charsets",
        "_files",
        "_access_control_request_headers",
        "_if_modified_since",
        "_if_unmodified_since",
        "_if_match",
        "_if_none_match",
        "_if_range",
        "_range",
        "_auth",
    )

    def __init__(
        self,
        method: str,
        path: str,
        query_string: str,
        headers: Headers | dict[str, str] | list[tuple[bytes, bytes]],
        body: bytes,
        transport: asyncio.Transport | None = None,
        app: Any = None,
        scope: dict | None = None,
        body_source: Any = None,
    ) -> None:
        # ASGI servers and `Veloce.add_route` already feed an uppercase
        # method; skip the allocation when the caller already complies.
        self.method = method if method.isupper() else method.upper()
        self.path = path
        self.query_string = query_string
        # Defer CIMultiDict construction (and the latin-1 decode of every
        # header tuple) until the handler actually reads `request.headers`.
        # The hot json-hello / path-param path never touches headers, so
        # eager construction was pure waste - 2-3 us per request.
        self._headers_raw: list[tuple[bytes, bytes]] | None = None
        if isinstance(headers, Headers):
            self._headers: Headers | None = headers
        elif (
            isinstance(headers, list) and headers and isinstance(headers[0][0], (bytes, bytearray))
        ):
            # ASGI raw `(bytes, bytes)` tuples - defer decode + Headers build.
            self._headers = None
            self._headers_raw = headers
        elif isinstance(headers, list) and not headers:
            # Empty list - cheap to materialize, no point deferring.
            self._headers = Headers()
        else:
            self._headers = Headers(headers)  # type: ignore[arg-type]
        # Body access is async (`await request.body()`). Two shapes:
        #  - in-memory (TestClient / ASGI): `body` holds the complete bytes
        #    the caller already buffered, so the first drain resolves
        #    immediately with no I/O and `_body_drained` starts True.
        #  - streamed (raw HTTP/1.1): `body_source` is a `RequestBodySource`
        #    fed by the protocol as bytes arrive; the body is not yet
        #    buffered, so `_body_drained` starts False and the first drain
        #    pulls the source to EOF. `_body` caches the assembled bytes once
        #    drained so sync accessors (`.data`, `get_json`) can serve them.
        self._body_source: Any = body_source
        if body_source is None:
            self._body: bytes = body
            self._body_drained: bool = True
        else:
            self._body = body
            self._body_drained = False
        # Set by a transport that has already applied this app's
        # MAX_CONTENT_LENGTH to both the declared and the received length, so
        # dispatch does not repeat the work on every request. It stays False for
        # a request built anywhere else - including the fresh one a mounted
        # sub-app is dispatched with, which must be checked against that app's
        # own limit rather than its parent's.
        self._length_enforced = False
        self.transport = transport
        self.app = app
        self.scope = scope or {}
        # `endpoint` is the matched route's name. Stays `None`
        # for synthetic requests built outside dispatch; Veloce
        # writes it inside `_dispatch_request` after `Router.match`.
        self.endpoint: str | None = None
        self._json: Any = _UNPARSED
        self._query_params: QueryParams | None = None
        self._form: Any = None
        self._path_params: dict[str, Any] = {}
        self._state: State = State()
        self._cookies: Cookies | None = None
        self._url: Any = None
        self._background_tasks: Any = None
        # Lazily-parsed `(mimetype, params)` pair for `Content-Type`.
        # `None` means "not yet parsed"; the parse happens at most once
        # per request, on first read of `mimetype` or `mimetype_params`.
        self._parsed_ct: tuple[str, dict[str, str]] | None = None
        self._accept_mimetypes: AcceptHeader | None = None
        self._accept_languages: AcceptHeader | None = None
        self._accept_encodings: AcceptHeader | None = None
        self._accept_charsets: AcceptHeader | None = None
        self._files: FormData | None = None
        # Conditional-header property caches. `_UNSET` marks "not yet
        # parsed" for the date-shaped properties whose legitimate
        # parsed value is `None` (absent or unparseable header). The
        # tuple-shaped properties use `None` itself as the cache miss.
        self._access_control_request_headers: list[str] | None = None
        self._if_modified_since: Any = _UNSET
        self._if_unmodified_since: Any = _UNSET
        self._if_match: tuple[str, ...] | None = None
        self._if_none_match: tuple[str, ...] | None = None
        self._if_range: tuple[str, float | None] | None = None
        self._range: Any = _UNSET
        self._auth: Any = _UNSET

    # ── Method, path and query ────────────────────────────
    @property
    def query_params(self) -> QueryParams:
        """Parse query string lazily - only when accessed.

        Repeated keys are preserved: `params.getlist("tag")` returns every
        value; `params["tag"]` returns the first.
        """
        if self._query_params is None:
            self._query_params = QueryParams.from_query_string(self.query_string)
        return self._query_params

    @property
    def args(self) -> QueryParams:
        """Alias for `query_params` — the parsed URL query string."""
        return self.query_params

    @property
    def path_params(self) -> dict[str, str]:
        """Path parameters captured by the matched route pattern."""
        return self._path_params

    @path_params.setter
    def path_params(self, value: dict[str, str]) -> None:
        self._path_params = value

    @property
    def view_args(self) -> dict[str, Any]:
        """Alias for `path_params` — the matched route's path params.

        `request.view_args` and `request.path_params` are two names for the
        dict of URL-captured values; both point at the same dict.
        """
        return self._path_params

    # ── Headers ───────────────────────────────────────────
    @property
    def headers(self) -> Headers:
        """Return the parsed request headers, materializing from raw ASGI tuples on first access."""
        h = self._headers
        if h is None:
            # Materialise from the raw ASGI tuples on first access. After
            # this point the cached `Headers` is reused; the raw list is
            # released so it can be garbage-collected.
            raw = self._headers_raw or ()
            h = Headers([(k.decode("latin-1"), v.decode("latin-1")) for k, v in raw])
            self._headers = h
            self._headers_raw = None
        return h

    @headers.setter
    def headers(self, value: Headers | dict[str, str] | list[tuple[str, str]]) -> None:
        self._headers = value if isinstance(value, Headers) else Headers(value)
        self._headers_raw = None

    @property
    def user_agent(self) -> str:
        """Return the User-Agent header value."""
        return self.headers.get(HEADER_USER_AGENT, "")

    @property
    def referrer(self) -> str:
        """Value of the `Referer` request header.

        Spelling preserved from the original RFC misprint (RFC 7231 Sec. 5.5.2
        documents `Referer`, not `Referrer`). The accessor uses the
        corrected spelling so callers don't have to remember.
        """
        return self.headers.get(HEADER_REFERER, "")

    @property
    def origin(self) -> str | None:
        """The `Origin` header - RFC 6454. `None` when absent.

        Set by browsers on cross-origin requests (and all CORS
        preflights). CORS middleware matches the allow-list against it.
        """
        return self.headers.get(HEADER_ORIGIN)

    @property
    def date(self) -> datetime | None:
        """The request `Date` header as a tz-aware UTC `datetime`.

        RFC 9110 Sec. 6.6.1 - the originator's timestamp for the message.
        Returns `None` when the header is missing or unparseable.
        """
        return parse_date(self.headers.get(HEADER_DATE))

    @property
    def pragma(self) -> str:
        """Value of the legacy `Pragma` header - RFC 9111 Sec. 5.4.

        Almost always `no-cache` from HTTP/1.0 clients. Returns the
        empty string when absent. Prefer `cache_control` for HTTP/1.1.
        """
        return (self.headers.get(HEADER_PRAGMA, "") or "").strip().lower()

    @property
    def max_forwards(self) -> int | None:
        """The `Max-Forwards` header as an int - RFC 9110 Sec. 7.6.2.

        Bounds how many proxies a TRACE/OPTIONS request may traverse.
        `None` when absent or non-numeric.
        """
        raw = (self.headers.get(HEADER_MAX_FORWARDS, "") or "").strip()
        return int(raw) if raw.isdigit() else None

    @property
    def is_xhr(self) -> bool:
        """Detect XMLHttpRequest-style AJAX calls.

        The convention is `X-Requested-With: XMLHttpRequest`, set by
        jQuery, fetch wrappers, and similar libraries. It's a hint, not
        a guarantee (the client controls the header), but it's the
        traditional signal application code uses to switch between full
        HTML responses and partial / JSON ones.
        """
        return self.headers.get(HEADER_X_REQUESTED_WITH, "").lower() == "xmlhttprequest"

    # ── Content type and body metadata ────────────────────
    @property
    def content_type(self) -> str:
        """Return the Content-Type header value, or an empty string."""
        return self.headers.get(HEADER_CONTENT_TYPE, "")

    def _parse_content_type(self) -> tuple[str, dict[str, str]]:
        """Parse `Content-Type` into `(mimetype, params)` and cache.

        `application/json; charset=utf-8` -> `("application/json", {"charset": "utf-8"})`.
        Same parser as the legacy split-on-each-property approach, but
        runs once per request instead of once per `mimetype` / `mimetype_params`
        access. Result is stashed on `_parsed_ct` for subsequent reads.
        """
        ct = self.content_type
        if not ct:
            return ("", {})
        mt, semi, rest = ct.partition(";")
        mimetype = mt.strip().lower()
        if not semi:
            # No parameters to walk: `partition` already said so, and most
            # content types carry none.
            return (mimetype, {})
        return (mimetype, dict(parse_media_type_params(rest)))

    @property
    def mimetype(self) -> str:
        """`Content-Type` without parameters.

        `application/json; charset=utf-8` -> `application/json`. Lower-cased
        and stripped - per RFC 9110 Sec. 8.3 the media type is case-insensitive.
        """
        if self._parsed_ct is None:
            self._parsed_ct = self._parse_content_type()
        return self._parsed_ct[0]

    @property
    def mimetype_params(self) -> dict[str, str]:
        """Parameters from `Content-Type` (e.g. `{"charset": "utf-8"}`).

        Each parameter is `key=value`; quoted values have their surrounding
        double-quotes stripped. Keys are lower-cased; values preserve case.
        """
        if self._parsed_ct is None:
            self._parsed_ct = self._parse_content_type()
        return self._parsed_ct[1]

    @property
    def content_length(self) -> int | None:
        """Return the Content-Length as an integer, or None."""
        raw = self._headers_raw
        if raw is not None:
            # The body-size guard reads this on every request, so going through
            # `self.headers` here would latin-1 decode and index every header
            # for a request whose handler may never read one - defeating the
            # deferral `__init__` sets up. Scan the raw tuples instead. The
            # length test rejects nearly every header before the comparison,
            # and ASGI servers send lower-cased names (the fallback covers a
            # transport that does not).
            cl_raw = None
            for k, v in raw:
                if len(k) == 14 and (k == b"content-length" or k.lower() == b"content-length"):
                    cl_raw = v
                    break
            if cl_raw is None:
                return None
            try:
                return int(cl_raw)
            except (ValueError, TypeError):
                return None
        cl = self.headers.get(HEADER_CONTENT_LENGTH)
        if not cl:
            return None
        try:
            return int(cl)
        except (ValueError, TypeError):
            return None

    @property
    def content_encoding(self) -> str:
        """Value of the `Content-Encoding` header.

        Returns the lowercased encoding name (`"gzip"`, `"br"`, etc.)
        or the empty string when the header is missing.
        """
        return (self.headers.get(HEADER_CONTENT_ENCODING, "") or "").strip().lower()

    @property
    def content_language(self) -> str:
        """Value of the `Content-Language` header - RFC 9110 Sec. 8.5.

        Returns the raw header value (a comma-separated list of
        language tags) or the empty string when the header is absent.
        """
        return self.headers.get(HEADER_CONTENT_LANGUAGE, "") or ""

    @property
    def charset(self) -> str:
        """Request body charset, decoded from `Content-Type`.

        Defaults to `utf-8` when no charset is declared (the modern
        default; the also moved off ISO-8859-1).
        """
        return self.mimetype_params.get("charset", "utf-8")

    @property
    def is_json(self) -> bool:
        """True for `application/json` or any `application/*+json` subtype.

        Per RFC 6839 Sec. 3.1 the structured-suffix `+json` (e.g.
        `application/vnd.api+json`, `application/problem+json`) marks the
        body as JSON-encoded.
        """
        return is_json_mimetype(self.mimetype)

    @property
    def is_multipart(self) -> bool:
        """`True` when the request body is `multipart/*`."""
        return self.mimetype.startswith("multipart/")

    @property
    def is_form(self) -> bool:
        """`True` when the body is `application/x-www-form-urlencoded`
        or `multipart/form-data`."""
        m = self.mimetype
        return m == MIME_FORM_URLENCODED or m.startswith("multipart/")

    # ── Content negotiation (Accept) ──────────────────────
    @property
    def accept(self) -> str:
        """Return the raw Accept header value."""
        return self.headers.get(HEADER_ACCEPT, "")

    @property
    def accept_mimetypes(self) -> AcceptHeader:
        """Parsed `Accept` header with MIME wildcard matching."""
        if self._accept_mimetypes is None:
            self._accept_mimetypes = AcceptHeader.parse(
                self.headers.get(HEADER_ACCEPT, ""), mime=True
            )
        return self._accept_mimetypes

    @property
    def accept_languages(self) -> AcceptHeader:
        """Parsed `Accept-Language` header. q-value ordered."""
        if self._accept_languages is None:
            self._accept_languages = AcceptHeader.parse(
                self.headers.get(HEADER_ACCEPT_LANGUAGE, "")
            )
        return self._accept_languages

    @property
    def accept_encodings(self) -> AcceptHeader:
        """Parsed `Accept-Encoding` header (e.g. gzip, br)."""
        if self._accept_encodings is None:
            self._accept_encodings = AcceptHeader.parse(
                self.headers.get(HEADER_ACCEPT_ENCODING, "")
            )
        return self._accept_encodings

    @property
    def accept_charsets(self) -> AcceptHeader:
        """Parsed `Accept-Charset` header."""
        if self._accept_charsets is None:
            self._accept_charsets = AcceptHeader.parse(self.headers.get(HEADER_ACCEPT_CHARSET, ""))
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
                # `_split_etag_list` keeps weak `W/` prefixes and does not break
                # on a comma inside an opaque tag's quoted string.
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
                # ETag values are quoted (optionally `W/"..."`). Anything else
                # is interpreted as an HTTP-date.
                if stripped.startswith('"') or stripped.startswith('W/"'):
                    cached = (stripped, None)
                else:
                    dt = parse_date(stripped)
                    cached = ("", None) if dt is None else ("", dt.timestamp())
            self._if_range = cached
        return cached

    @property
    def range(self) -> RangeSpec | None:
        """Parse `Range:` header per RFC 9110 Sec. 14.2. Returns `None` when
        absent or unparseable."""
        cached = self._range
        if cached is _UNSET:
            cached = RangeSpec.parse(self.headers.get(HEADER_RANGE, ""))
            self._range = cached
        return cached

    # ── Caching directives ────────────────────────────────
    @property
    def cache_control(self) -> Any:
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
            # A client may legitimately send multiple `Cookie` headers
            # (RFC 6265). Merge them with the `; ` delimiter so every
            # cookie parses; keep the single-header fast path index-only.
            parts = self.headers.getlist(HEADER_COOKIE)
            if not parts:
                header = ""
            elif len(parts) == 1:
                header = parts[0]
            else:
                header = "; ".join(parts)
            self._cookies = Cookies.from_cookie_header(header)
        return self._cookies

    # ── URL and routing ───────────────────────────────────
    @property
    def url(self) -> Any:
        """Full URL object - lazy construction."""
        if self._url is None:
            scope = getattr(self, "scope", None)
            scope_scheme = scope.get("scheme") if isinstance(scope, dict) else None
            # The raw transport carries no ASGI scope, so nothing else can
            # tell this request it arrived over TLS: `app.run(ssl_context=)`
            # and the gunicorn worker both terminate TLS on the connection
            # itself. Without this the scheme fell through to plain "http" on a
            # genuinely encrypted connection, so `url_for(_external=True)`
            # emitted http:// and any `scheme == "https"` check failed. The live
            # connection outranks `X-Forwarded-Proto` below: a header cannot be
            # more authoritative than the socket it arrived on.
            if (
                scope_scheme is None
                and self.transport is not None
                and self.transport.get_extra_info("ssl_object") is not None
            ):
                scope_scheme = URL_SCHEME_HTTPS
            # A trusted ProxyFix hop stashes the public port here when the
            # forwarded Host carries none (e.g. proxy sends X-Forwarded-Host
            # without a port plus a separate X-Forwarded-Port: 8443).
            state = self._state
            forwarded_port = state.get("proxy_fix_port") if state else None
            self._url = URL.from_request(
                self.headers,
                self.path,
                self.query_string,
                scope_scheme=scope_scheme,
                forwarded_port=forwarded_port,
                trust_forwarded_proto=not (state and "proxy_fix_applied" in state),
            )
        return self._url

    @property
    def base_url(self) -> str:
        """Base URL (scheme + host)."""
        url = self.url
        return f"{url.scheme}://{url.netloc}"

    @property
    def full_path(self) -> str:
        """Path + `?` + query string. Always contains a `?` even when the
        query string is empty."""
        return f"{self.path}?{self.query_string}"

    @property
    def url_root(self) -> str:
        """Root URL of the request: `scheme://host/` (with trailing slash,
        no path or query string)."""
        url = self.url
        return f"{url.scheme}://{url.netloc}/"

    @property
    def host_url(self) -> str:
        """Alias for `url_root` - Veloce exposes both names."""
        return self.url_root

    @property
    def is_secure(self) -> bool:
        """Return True if the request uses HTTPS."""
        return self.url.scheme == URL_SCHEME_HTTPS

    @property
    def scheme(self) -> str:
        """Request scheme - `"http"` or `"https"`.

        Sourced from the ASGI `scope["scheme"]` when present, then from
        the `X-Forwarded-Proto` header (only meaningful behind a trusted
        proxy), then default `http`.
        """
        return self.url.scheme

    @property
    def host(self) -> str:
        """Value of the `Host` request header.

        Mirrors `Request.url.netloc` for the common case but pulls
        directly from the header to remain cheap (no full URL parse).
        Returns the empty string when the header is absent.
        """
        return self.headers.get(HEADER_HOST, "")

    @property
    def root_path(self) -> str:
        """ASGI `scope["root_path"]` - the URL prefix the app is mounted under.

        Comes from the ASGI server (e.g. uvicorn `--root-path /api`) or
        from `app.mount("/sub", inner_app)`. Used so an app behind a
        prefix can generate correct external URLs without knowing the
        prefix at code-time.

        Returns the empty string when the app is at root.
        """
        return self.scope.get("root_path", "") if isinstance(self.scope, dict) else ""

    @property
    def script_root(self) -> str:
        """Alias for `root_path` — also called `script_root`.

        ProxyFix-style middleware may also set
        `_state["proxy_fix_prefix"]`; that wins over the ASGI scope
        because it represents the *trusted* outer-edge prefix when the
        ASGI server is behind a reverse proxy that strips the prefix.
        """
        proxied = self._state.get("proxy_fix_prefix")
        if proxied:
            return proxied
        return self.root_path

    @property
    def subdomain(self) -> str:
        """Leftmost host label minus `app.config["SERVER_NAME"]`.

        Returns the empty string when the request host equals
        `SERVER_NAME` exactly (apex), or when `SERVER_NAME` isn't
        configured and the host has no dots. With `SERVER_NAME` set,
        the returned value is the prefix that wouldn't match the
        configured apex; without it, the leftmost label.
        """
        host = (self.host or "").split(":", 1)[0].lower()
        if not host:
            return ""
        app = getattr(self, "app", None)
        cfg = getattr(app, "config", None) if app is not None else None
        server_name = (cfg.get("SERVER_NAME") if cfg else "") or ""
        server_name = server_name.lower()
        if server_name:
            if host == server_name:
                return ""
            if host.endswith("." + server_name):
                return host[: -(len(server_name) + 1)]
            return ""
        # No SERVER_NAME - return the leftmost label only when the host
        # has more than one label (otherwise it's the apex).
        if "." not in host:
            return ""
        return host.split(".", 1)[0]

    @property
    def environ(self) -> dict[str, Any]:
        """Alias for the ASGI `scope` dict.

        Third-party code paths reach for `request.environ` (WSGI); ASGI
        scope is the analogue. Returns the live dict so middleware can
        introspect (mutation goes through framework APIs, not this).
        """
        return self.scope if isinstance(self.scope, dict) else {}

    # ── Matched route and reverse URLs ─────────────────────

    @property
    def url_rule(self) -> str | None:
        """Return the matched route's template (e.g. `/users/{id}`).

        Returns the raw path template the radix tree used for the match —
        i.e. `path_params` placeholders are unsubstituted. `None` for
        synthetic requests that never went through dispatch.
        """
        return self._state.get("url_rule") if self._state else None

    @property
    def is_mcp(self) -> bool:
        """Return whether this request is a replayed MCP tool / resource call.

        `True` when the request was synthesised by the MCP integration to replay a
        route through `Depends` / middleware for an agent call, rather than a real
        HTTP request. Authentication middleware that checks a browser credential
        (a session cookie, an `Authorization` header) should return early on these
        - the MCP transport authenticates the agent separately. The transport
        request itself (`POST /mcp`) opts such middleware out via
        `mount_mcp(..., exclude_middleware=[...])`.
        """
        return bool(self._state.get("_mcp")) if self._state else False

    @property
    def blueprint(self) -> str | None:
        """Return the name of the blueprint that owns the matched route.

        Veloce stores the endpoint as `<bp>.<name>` for blueprint routes.
        Returns the bit before the dot, or `None` if the endpoint is
        unset or is a top-level (no-dot) name.
        """
        ep = self.endpoint
        if not ep or "." not in ep:
            return None
        return ep.rsplit(".", 1)[0]

    @property
    def blueprints(self) -> list[str]:
        """Return every blueprint in the matched endpoint's parent chain.

        For an endpoint `a.b.c.view`, returns `["a.b.c", "a.b", "a"]`
        (innermost first). Empty list when the route is top-level or the
        endpoint is unset.
        """
        ep = self.endpoint
        if not ep or "." not in ep:
            return []
        parts = ep.rsplit(".", 1)[0].split(".")
        return [".".join(parts[: i + 1]) for i in range(len(parts) - 1, -1, -1)]

    def url_for(self, name: str, /, **path_params: Any) -> str:
        """Reverse-resolve a route URL - ASGI shape.

        `request.url_for("route_name", id=7)` delegates to the bound app's
        `url_for`. Raises `RuntimeError` when the request has no app bound
        (synthetic requests built outside dispatch).

        With `_external=True` the absolute URL is built from *this request's*
        origin - the scheme, host and port a trusted `ProxyFix` recovered - and
        carries `script_root`, so a link generated behind a proxy that
        terminates TLS on another port and mounts the app under a prefix points
        at the public URL rather than the internal one. `app.url_for` has no
        request to read and falls back to `SERVER_NAME`. An explicit `_scheme`
        or `_host` still wins.
        """
        if self.app is None:
            raise RuntimeError(
                "Request.url_for requires a bound app; this request was "
                "constructed outside the dispatch pipeline"
            )
        if not path_params.pop("_external", False):
            return self.app.url_for(name, **path_params)
        scheme = path_params.pop("_scheme", None)
        host = path_params.pop("_host", None)
        # The app builds the path (with any query string and anchor); the
        # origin and mount prefix come from the request.
        path = self.app.url_for(name, **path_params)
        url = self.url
        root = self.script_root.rstrip("/")
        return f"{scheme or url.scheme}://{host or url.netloc}{root}{path}"

    # ── Client and connection ─────────────────────────────
    @property
    def client_host(self) -> str | None:
        """Return the client's IP address, or `None` when the peer is unknown."""
        # If a ProxyFix-style middleware ran upstream, it stashed the
        # trusted client IP on `_state`. Prefer that over the raw TCP peer.
        proxied = self._state.get("proxy_fix_client")
        if proxied:
            return proxied
        if self.transport:
            peername = self.transport.get_extra_info("peername")
            if peername:
                return peername[0]
        # ASGI path - the peer lives on the scope, not a transport. Reading it
        # here (rather than in each caller) is what keeps `remote_addr` and the
        # rate limiter's client bucket from silently degrading under an ASGI
        # server, where `transport` is always None.
        client = self.scope.get("client") if self.scope else None
        return client[0] if client else None

    @property
    def client_port(self) -> int | None:
        """Return the client's port number, or `None` when the peer is unknown."""
        if self.transport:
            peername = self.transport.get_extra_info("peername")
            if peername and len(peername) >= 2:
                return peername[1]
        client = self.scope.get("client") if self.scope else None
        return client[1] if client and len(client) >= 2 else None

    @property
    def client(self) -> Address | None:
        """The connecting peer as an `Address(host, port)`.

        `request.client.host` / `request.client.port` work, and tuple
        unpacking (`host, port = request.client`) works too. Returns
        `None` when the peer is unknown (e.g. synthetic requests).
        Honours ProxyFix - `client.host` reflects the trusted client IP.
        """
        host = self.client_host
        if host is None:
            return None
        return Address(host, self.client_port or 0)

    @property
    def remote_addr(self) -> str | None:
        """Alias for `client_host` — the connecting client's IP.

        Honours ProxyFix-style middleware: when the trusted hop has set
        `_state["proxy_fix_client"]`, that value wins over the raw TCP
        peer (the ASGI/uvicorn `client[0]` may be the load balancer).
        """
        return self.client_host

    @property
    def access_route(self) -> list[str]:
        """Forwarded-for chain.

        Returns the comma-separated `X-Forwarded-For` values (client ->
        proxy chain order), with the connecting peer (`remote_addr`)
        appended at the end. With no `X-Forwarded-For` header, returns
        `[remote_addr]` when the peer is known, else `[]`.

        RFC 7239 Sec. 5.2 defines the IP-order convention: leftmost is the
        originating client, rightmost is the closest proxy. Production
        code should consume the leftmost *trusted* entry, not blindly
        the leftmost value.
        """
        forwarded = self.headers.get(HEADER_X_FORWARDED_FOR, "")
        chain: list[str] = [v.strip() for v in forwarded.split(",") if v.strip()]
        peer = self.client_host
        if peer:
            chain.append(peer)
        return chain

    # ── Request state and session ─────────────────────────
    @property
    def state(self) -> State:
        """Per-request scratch namespace - ASGI shape.

        Supports attribute access (`request.state.user = ...`) *and*
        dict access (`request.state["user"]`, `request.state.get(...)`).
        """
        return self._state

    @property
    def session(self) -> dict[str, Any]:
        """Access to the session dict.

        `SessionMiddleware` writes the parsed session into `_state["session"]`
        during `process_request`. This property surfaces it under the
        a convenience accessor. Raises `RuntimeError` when the middleware hasn't
        run - keeps "I forgot to add SessionMiddleware" from showing up
        as a confusing silent empty-dict.
        """
        if "session" not in self._state:
            raise RuntimeError(
                "Request.session is unavailable - install SessionMiddleware "
                "(`app.add_middleware(SessionMiddleware(secret_key=...))`) "
                "to enable session storage."
            )
        session = self._state["session"]
        # Mark the session accessed so the middleware emits `Vary: Cookie`
        # only when a handler actually touched session contents (read or
        # write), keeping session-independent responses cacheable. A plain
        # dict placed here by other code has no `accessed` slot, so guard it.
        if hasattr(session, "accessed"):
            session.accessed = True
        return session

    # ── Body access ───────────────────────────────────────
    @property
    def data(self) -> bytes:
        """Raw request body bytes - `request.data` shape.

        The sync-property form of `body()`. Returns the body exactly as
        received, with no decoding or form parsing. Requires the body to
        already be buffered; raises `RuntimeError` otherwise (use
        `await request.body()` for the async path).
        """
        if not self._body_drained:
            raise RuntimeError(
                "Request body is not yet buffered; use `await request.body()` "
                "instead of the sync `request.data` accessor"
            )
        return self._body

    def _config(self) -> Any:
        """Return the bound app's `config` mapping, or `None`.

        `getattr(None, ...)` is safe, so this also covers the unbound-app
        case without a separate guard. Cold path - only the body/form/json
        accessors read per-app limit knobs.
        """
        return getattr(self.app, "config", None)

    @property
    def max_content_length(self) -> int | None:
        """The body-size cap for this request.

        Reads `app.config["MAX_CONTENT_LENGTH"]` from the bound app
        (the dispatcher enforces it, returning 413 on overflow).
        `None` - no limit - when unset or no app is bound.
        """
        cfg = self._config()
        return cfg.get("MAX_CONTENT_LENGTH") if cfg is not None else None

    def get_json(
        self,
        force: bool = False,
        silent: bool = False,
        cache: bool = True,
    ) -> Any:
        """Parse the request body as JSON.

        - `force=True` skips the `is_json` content-type check; useful when
          the client sends JSON without setting `Content-Type` (e.g. some
          XHR libraries). Default is to honour the content type and return
          `None` for non-JSON requests.
        - `silent=True` swallows `orjson.JSONDecodeError` and returns `None`.
          Default raises so caller code can distinguish malformed JSON
          from missing JSON.
        - `cache=False` forces a re-parse on every call. Default caches
          the parsed value (one parse per request); cache invalidation
          is the caller's job when `cache=False`.

        Returns `None` for empty bodies regardless of `force` / `silent`.

        This is the synchronous accessor: it requires the
        body to already be buffered (the in-memory path), and raises
        `RuntimeError` otherwise - reach for `await request.json()` when
        the body has not yet been drained.
        """
        if not self._body_drained:
            raise RuntimeError(
                "Request body is not yet buffered; use `await request.json()` "
                "instead of the sync `request.get_json()` accessor"
            )
        if not force and not self.is_json:
            return None
        body = self._body
        if not body:
            return None

        if cache and self._json is not _UNPARSED:
            return self._json

        try:
            parsed = orjson.loads(body)
        except orjson.JSONDecodeError as exc:
            if silent:
                return None
            return self.on_json_loading_failed(exc)

        if cache:
            self._json = parsed
        return parsed

    def on_json_loading_failed(self, error: Exception) -> Any:
        """Hook invoked when JSON parsing fails on a non-silent body.

        Raises `BadRequest` (400) with a stable, body-independent message so a
        malformed body cannot leak decoder internals (byte offsets derived from
        attacker-controlled input) into the production response. The verbose
        decoder reason is always logged and attached as `BadRequest.debug_detail`
        for operators, and is surfaced in the response only when debug mode or
        the `JSON_ERRORS_VERBOSE` config flag is set. Override on a `Request`
        subclass to customise.
        """
        _logger.warning("JSON parse error: %s", error)
        cfg = self._config()
        # Surface the verbose reason when explicitly enabled OR in debug mode.
        # An explicit OR (not a dict-default fallback) is required because the
        # `JSON_ERRORS_VERBOSE` key is seeded into the default config, so it is
        # never "absent" for the fallback to consult `DEBUG`. `_coerce_bool`
        # interprets dotenv-style string flags (`DEBUG=false`) correctly.
        verbose = (
            _coerce_bool(cfg.get("JSON_ERRORS_VERBOSE", False))
            or _coerce_bool(cfg.get("DEBUG", False))
            if cfg
            else False
        )
        detail = f"Invalid JSON body: {error}" if verbose else "Invalid JSON body"
        exc = BadRequest(detail)
        # Informational only - never read by the error renderer, so it cannot
        # leak into the response body. `HTTPException` is unslotted, so this
        # dynamic attribute is set via `setattr` (not a declared field).
        setattr(exc, "debug_detail", str(error))  # noqa: B010
        raise exc from error

    # ── Async body, form, and streaming ────────────────────

    def _mark_body_buffered(self) -> None:
        """Publish an already-complete body without awaiting the source.

        The raw transport builds a `Request` at headers-complete and only then
        feeds the body, so a non-streaming route has to buffer before dispatch
        for the sync `.data` / `.get_json()` / `.form` accessors to see it. When
        the whole request arrived in one segment - the common case - the source
        is already at EOF with nothing queued, and awaiting it would cost a
        coroutine per request to learn there is nothing to wait for. The caller
        checks `source.at_eof` and calls this instead.
        """
        self._body_drained = True

    async def _drain_body(self) -> bytes:
        """Pull the body source to EOF once and cache the assembled bytes.

        In-memory requests are pre-filled at construction, so the first
        drain resolves immediately. Streamed requests (raw HTTP/1.1) pull the
        `RequestBodySource` to EOF here. The result is cached on `_body`;
        later drains return the cache.
        """
        if not self._body_drained:
            if self._body_source is not None:
                self._body = await self._body_source.read()
            self._body_drained = True
        return self._body

    async def body(self) -> bytes:
        """Return the full request body as bytes, draining the source once.

        Async to match the ASGI convention. Veloce buffers the
        body before dispatch, so no I/O happens inside the await - the
        coroutine resolves immediately with the cached bytes.
        """
        return await self._drain_body()

    async def json(self) -> Any:
        """Parse the request body as JSON, async to match the ASGI convention.

        Veloce buffers the body at construction time, so no I/O actually
        happens inside the await - the coroutine resolves immediately
        with the cached parse. The async signature exists so the
        `await request.json()` idiom does not blow up at runtime.

        The synchronous `request.get_json()` accessor is available for
        callers that prefer a sync API.
        """
        if self._json is _UNPARSED:
            body = await self._drain_body()
            if not body:
                self._json = None
            else:
                try:
                    self._json = orjson.loads(body)
                except (orjson.JSONDecodeError, ValueError) as exc:
                    # Share the single masking policy with the sync `get_json`.
                    return self.on_json_loading_failed(exc)
        return self._json

    async def get_data(self, as_text: bool = False, cache: bool = True) -> bytes | str:
        """Return the raw request body, draining the source once.

        Async to match `body()` / `json()` / `form()`.

        - `as_text=True` decodes via the `Content-Type` charset (default
          UTF-8). Falls back to `latin-1` when the declared charset is
          unrecognised - a defensive fallback, since
          latin-1 round-trips arbitrary bytes without raising.
        - `cache` is accepted and ignored. A non-streaming route has its body
          buffered before the handler runs, so there is nothing to decide; a
          `stream=True` route is consumed through `request.stream()` rather
          than here. The parameter is kept so callers passing `cache=False`
          for cross-framework compatibility keep working.

        Returns `bytes` (default) or `str` (with `as_text=True`).
        """
        _ = cache  # accepted for compatibility; see the note above
        body = await self._drain_body()
        if not as_text:
            return body
        charset = self.mimetype_params.get("charset", "utf-8")
        try:
            return body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            return body.decode("latin-1")

    async def form(self) -> Any:
        """Parse form data including file uploads."""
        if self._form is None:
            mt = self.mimetype
            if mt == MIME_FORM_URLENCODED:
                body = await self._drain_body()
                # Cap the field count so a pathological body cannot exhaust
                # memory/CPU even when its total size is within
                # MAX_CONTENT_LENGTH. Shares the MAX_FORM_PARTS knob with the
                # multipart branch; `None` disables the cap.
                max_fields = DEFAULT_MAX_MULTIPART_PARTS
                cfg = self._config()
                if cfg is not None:
                    max_fields = cfg.get("MAX_FORM_PARTS", max_fields)
                # Decode outside the field-count guard below: a non-UTF-8 body
                # is a malformed request (400), not a field-count overflow.
                # `UnicodeDecodeError` subclasses `ValueError`, so leaving it
                # inside the `except ValueError` would misreport it as a 413.
                try:
                    decoded = body.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise BadRequest("form body is not valid UTF-8") from exc
                try:
                    items = _parse_qs_pairs(decoded, max_fields)
                except ValueError as exc:
                    raise RequestEntityTooLarge(
                        f"form exceeds the {max_fields}-field limit"
                    ) from exc
                self._form = FormData(items)
            elif mt == MIME_MULTIPART_FORM_DATA:
                # Per-app multipart caps come from config when an app is
                # bound; otherwise the module defaults apply.
                max_parts = DEFAULT_MAX_MULTIPART_PARTS
                max_part_size = DEFAULT_MAX_MULTIPART_PART_SIZE
                mp_max_files: int | None = None
                mp_max_fields: int | None = None
                mp_max_file_size: int | None = None
                mp_max_field_size: int | None = None
                mp_max_field_memory: int | None = None
                cfg = self._config()
                if cfg is not None:
                    cfg_parts = cfg.get("MAX_FORM_PARTS", max_parts)
                    if cfg_parts is not None:
                        max_parts = cfg_parts
                    cfg_part_size = cfg.get("MAX_FORM_PART_SIZE", max_part_size)
                    if cfg_part_size is not None:
                        max_part_size = cfg_part_size
                    mp_max_files = cfg.get("MAX_FORM_FILES")
                    mp_max_fields = cfg.get("MAX_FORM_FIELDS")
                    mp_max_file_size = cfg.get("MAX_FORM_FILE_SIZE")
                    mp_max_field_size = cfg.get("MAX_FORM_FIELD_SIZE")
                    mp_max_field_memory = cfg.get("MAX_FORM_FIELD_MEMORY")
                multipart_body = await self._drain_body()
                self._form = parse_multipart_form(
                    multipart_body,
                    self.content_type,
                    max_parts=max_parts,
                    max_files=mp_max_files,
                    max_fields=mp_max_fields,
                    max_part_size=max_part_size,
                    max_file_size=mp_max_file_size,
                    max_field_size=mp_max_field_size,
                    max_field_memory=mp_max_field_memory,
                )
            else:
                self._form = FormData()
        return self._form

    async def files(self) -> Any:
        """View of uploaded files only - a `FormData` subset.

        Parses the form (via `form()`) and returns a `FormData`
        containing just the entries whose value is an `UploadFile`.
        Non-file form fields are excluded. Empty `FormData` for
        non-multipart requests. Result is cached after first parse.
        """
        if self._files is not None:
            return self._files
        form = await self.form()
        files = FormData()
        non_file_keys: set[str] = set()
        for key, value in form.items():
            if isinstance(value, UploadFile):
                files.add(key, value)
            else:
                non_file_keys.add(key)
        # In debug mode, record what the request actually carried so a
        # missing-key lookup on `request.files` raises a descriptive
        # `FilesKeyError` (e.g. "submitted as a plain form field, add
        # enctype=multipart/form-data") instead of a bare `KeyError`.
        # Gated on `app.debug` so production lookups keep plain semantics.
        if self.app is not None and getattr(self.app, "debug", False):
            files._files_diagnostic = (self.mimetype, frozenset(non_file_keys))
        self._files = files
        return self._files

    async def values(self) -> Any:
        """Merged query string + form body - `request.values` shape.

        Returns a fresh `MultiDict` with query-string entries first,
        then form-body entries appended. Both source `MultiDict`s
        preserve repeated keys; merging preserves the order across
        both sources. Form parsing is async (multipart may need
        executor reads), so this is an awaitable rather than a property.
        """
        merged: MultiDict = MultiDict()
        for k, v in self.query_params.items():
            merged.add(k, v)
        form = await self.form()
        if form is not None:
            for k, v in form.items():
                merged.add(k, v)
        return merged

    async def text(self) -> str:
        """Return body as text, draining the source once."""
        body = await self._drain_body()
        return body.decode("utf-8", errors="replace")

    def _close_uploads(self) -> None:
        """Close the spool files this request's multipart parse opened.

        An upload larger than the spool threshold is a real file on disk, and
        nothing closed it once the response was sent - the descriptor and the
        temp file survived until the garbage collector happened to reach the
        `UploadFile`. Under sustained upload traffic that is descriptor
        exhaustion. Called once the request is finished with, which is after
        any background task has run.
        """
        form = self._form
        if form is None:
            return
        for value in form.values():
            close = getattr(value, "file", None)
            if close is not None and not getattr(close, "closed", True):
                with contextlib.suppress(Exception):
                    close.close()

    async def is_disconnected(self) -> bool:
        """Whether the client has disconnected.

        A non-streaming route has its body drained before the handler runs, so
        the body is already received and the answer is always `False`. On a
        `stream=True` route the client can genuinely go away mid-handler, and
        this reports it once the consumer has seen the disconnect - reading the
        flag the body source records rather than probing the transport.
        """
        source = self._body_source
        return bool(source is not None and getattr(source, "_disconnected", False))

    async def stream(self) -> Any:
        """Async-iterate the request body in chunks - ASGI shape.

        Streamed requests (raw HTTP/1.1) yield each chunk as the socket
        delivers it, so `async for chunk in request.stream(): ...` processes
        a large body incrementally without ever buffering it whole. For
        in-memory requests (TestClient / ASGI), or once a streamed body has
        already been drained and cached, the buffered bytes are sliced into
        64 KiB chunks instead.
        """
        if self._body_source is not None and not self._body_drained:
            # Pull live from the source so a streaming handler observes
            # chunks at the cadence the protocol feeds them. Cache as we go
            # so a later `.body()` / `.data` still sees the full payload.
            parts: list[bytes] = []
            async for chunk in self._body_source:
                parts.append(chunk)
                yield chunk
            self._body = b"".join(parts)
            self._body_drained = True
            return
        body = await self._drain_body()
        chunk_size = 65536
        for offset in range(0, len(body), chunk_size):
            yield body[offset : offset + chunk_size]
