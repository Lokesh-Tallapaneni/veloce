"""Request object - lazy parsing for speed."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import parse_qsl

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
    MIME_JSON,
    MIME_MULTIPART_FORM_DATA,
)
from veloce._internal import _coerce_bool
from veloce._protocol_constants import URL_SCHEME_HTTPS
from veloce.exceptions import RequestEntityTooLarge
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
    parse_multipart_form,
)
from veloce.http.dates import parse_date

_logger = logging.getLogger(__name__)

# Sentinel for "this conditional-header property has not been read yet"
# on properties whose legitimate parsed value can be `None` (`if_modified_since`,
# `if_unmodified_since`). Distinct from `None` so we can tell "absent
# header" apart from "haven't looked yet".
_UNSET: Any = object()


class Request:
    """Incoming HTTP request with lazy attribute parsing.

    All expensive operations (JSON parsing, cookie parsing, URL construction,
    form/multipart parsing) are deferred until accessed - zero overhead for
    properties you don't use.
    """

    __slots__ = (
        "method",
        "path",
        "query_string",
        "_headers",
        "_headers_raw",
        "_body",
        "_body_drained",
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
        self.transport = transport
        self.app = app
        self.scope = scope or {}
        # `endpoint` is the matched route's name. Stays `None`
        # for synthetic requests built outside dispatch; Veloce
        # writes it inside `_dispatch_request` after `Router.match`.
        self.endpoint: str | None = None
        self._json: Any = None
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

    # -- Method, path and query -----------------------------------------
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
        """an alias for `query_params` - the parsed URL query string."""
        return self.query_params

    @property
    def path_params(self) -> dict[str, str]:
        return self._path_params

    @path_params.setter
    def path_params(self, value: dict[str, str]) -> None:
        self._path_params = value

    @property
    def view_args(self) -> dict[str, Any]:
        """an alias for `path_params` - the matched route's path params.

        Veloce names the dict of URL-captured values `request.view_args`;
        veloce calls it `path_params`. Both point at the same dict.
        """
        return self._path_params

    # -- Headers --------------------------------------------------------
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
    def date(self) -> Any:
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

    # -- Content type and body metadata ---------------------------------
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
        mt, _, rest = ct.partition(";")
        mimetype = mt.strip().lower()
        params: dict[str, str] = {}
        if rest:
            for chunk in rest.split(";"):
                chunk = chunk.strip()
                if "=" not in chunk:
                    continue
                k, _, v = chunk.partition("=")
                v = v.strip()
                if v.startswith('"') and v.endswith('"') and len(v) >= 2:
                    v = v[1:-1]
                params[k.strip().lower()] = v
        return (mimetype, params)

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
        ct = self.mimetype
        if ct == MIME_JSON:
            return True
        return ct.startswith("application/") and ct.endswith("+json")

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

    # -- Content negotiation (Accept) -----------------------------------
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

    # -- Authorization --------------------------------------------------
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

    # -- CORS preflight (RFC 6454) --------------------------------------
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

    # -- Conditional requests (RFC 9110 Sec. 13) ----------------------------
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
                if stripped == "*":
                    cached = ("*",)
                else:
                    cached = tuple(t.strip() for t in stripped.split(",") if t.strip())
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
                if stripped == "*":
                    cached = ("*",)
                else:
                    # Comma-separated, optionally with weak `W/` prefixes.
                    cached = tuple(t.strip() for t in stripped.split(",") if t.strip())
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

    # -- Caching directives ---------------------------------------------
    @property
    def cache_control(self) -> Any:
        """Parsed `Cache-Control` header.

        Returns a `CacheControl` view: `req.cache_control.no_cache`
        (bool), `req.cache_control.max_age` (int or None), etc.
        Always returns a fresh parse to reflect any header mutation.
        """
        return CacheControl(self.headers.get(HEADER_CACHE_CONTROL, ""))

    # -- Cookies --------------------------------------------------------
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

    # -- URL and routing ------------------------------------------------
    @property
    def url(self) -> Any:
        """Full URL object - lazy construction."""
        if self._url is None:
            scope = getattr(self, "scope", None)
            scope_scheme = scope.get("scheme") if isinstance(scope, dict) else None
            self._url = URL.from_request(
                self.headers, self.path, self.query_string, scope_scheme=scope_scheme
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
        """an alias for `root_path` - also called `script_root`.

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
    def environ(self) -> dict:
        """Alias for the ASGI `scope` dict.

        third-party code paths reach for `request.environ` (WSGI); ASGI
        scope is the analogue. Returns the live dict so middleware can
        introspect (mutation goes through framework APIs, not this).
        """
        return self.scope if isinstance(self.scope, dict) else {}

    @property
    def url_rule(self) -> str | None:
        """the matched route's template (e.g. `/users/{id}`).

        Returns the raw path template the radix tree used for the match -
        i.e. `path_params` placeholders are unsubstituted. `None` for
        synthetic requests that never went through dispatch.
        """
        return self._state.get("url_rule") if self._state else None

    @property
    def blueprint(self) -> str | None:
        """the name of the blueprint that owns the matched route.

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
        """every blueprint in the matched endpoint's parent chain.

        For an endpoint `a.b.c.view`, returns `["a.b.c", "a.b", "a"]`
        (innermost first). Empty list when the route is top-level or the
        endpoint is unset.
        """
        ep = self.endpoint
        if not ep or "." not in ep:
            return []
        parts = ep.rsplit(".", 1)[0].split(".")
        return [".".join(parts[: i + 1]) for i in range(len(parts) - 1, -1, -1)]

    def url_for(self, name: str, **path_params: Any) -> str:
        """Reverse-resolve a route URL - ASGI shape.

        `request.url_for("route_name", id=7)` delegates to the bound
        app's `url_for`. Raises `RuntimeError` when the request has no
        app bound (synthetic requests built outside dispatch).
        """
        if self.app is None:
            raise RuntimeError(
                "Request.url_for requires a bound app; this request was "
                "constructed outside the dispatch pipeline"
            )
        return self.app.url_for(name, **path_params)

    # -- Client and connection ------------------------------------------
    @property
    def client_host(self) -> str | None:
        """Return the client's IP address from the ASGI scope."""
        # If a ProxyFix-style middleware ran upstream, it stashed the
        # trusted client IP on `_state`. Prefer that over the raw TCP peer.
        proxied = self._state.get("proxy_fix_client")
        if proxied:
            return proxied
        if self.transport:
            peername = self.transport.get_extra_info("peername")
            if peername:
                return peername[0]
        return None

    @property
    def client_port(self) -> int | None:
        """Return the client's port number from the ASGI scope."""
        if self.transport:
            peername = self.transport.get_extra_info("peername")
            if peername and len(peername) >= 2:
                return peername[1]
        return None

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
            # ASGI scope path - `scope["client"]` is `(host, port)`.
            scope_client = self.scope.get("client") if self.scope else None
            if scope_client:
                return Address(scope_client[0], scope_client[1])
            return None
        port = self.client_port or 0
        return Address(host, port)

    @property
    def remote_addr(self) -> str | None:
        """an alias for `client_host` - the connecting client's IP.

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
        if not peer:
            # ASGI scope path - `scope["client"]` is `(host, port)` per spec.
            client = self.scope.get("client") if self.scope else None
            if client:
                peer = client[0]
        if peer:
            chain.append(peer)
        return chain

    # -- Request state and session --------------------------------------
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

    # -- Body access ----------------------------------------------------
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

    @property
    def max_content_length(self) -> int | None:
        """The body-size cap for this request.

        Reads `app.config["MAX_CONTENT_LENGTH"]` from the bound app
        (the dispatcher enforces it, returning 413 on overflow).
        `None` - no limit - when unset or no app is bound.
        """
        if self.app is None:
            return None
        cfg = getattr(self.app, "config", None)
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

        This is the synchronous, Flask-flavoured accessor: it requires the
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

        if cache and self._json is not None:
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
        from veloce.exceptions import BadRequest  # noqa: I001 - breaks cycle: exceptions -> app -> request

        _logger.warning("JSON parse error: %s", error)
        cfg = getattr(self.app, "config", None) if self.app is not None else None
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

        Async to match Starlette / FastAPI / Quart. Veloce buffers the
        body before dispatch, so no I/O happens inside the await - the
        coroutine resolves immediately with the cached bytes.
        """
        return await self._drain_body()

    async def json(self) -> Any:
        """Parse the request body as JSON, async to match Starlette / FastAPI / Quart.

        Veloce buffers the body at construction time, so no I/O actually
        happens inside the await - the coroutine resolves immediately
        with the cached parse. The async signature exists so the
        `await request.json()` idiom Starlette and FastAPI callers
        reach for first does not blow up at runtime.

        For Flask muscle-memory call `request.get_json()` instead - that
        remains synchronous to match Flask's `Request.get_json`.
        """
        if self._json is None:
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
        - `cache=True` is a no-op today (veloce already buffers the
          whole body on construction) but keeps the parameter so
          callers that pass `cache=False` for streaming compatibility
          don't break. Streaming-body support arrives separately.

        Returns `bytes` (default) or `str` (with `as_text=True`).
        """
        _ = cache  # reserved for streaming-body work; no caching needed yet
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
                cfg = getattr(self.app, "config", None) if self.app is not None else None
                if cfg is not None:
                    max_fields = cfg.get("MAX_FORM_PARTS", max_fields)
                try:
                    items = parse_qsl(
                        body.decode("utf-8"),
                        keep_blank_values=True,
                        max_num_fields=max_fields,
                    )
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
                cfg = getattr(self.app, "config", None) if self.app is not None else None
                if cfg is not None:
                    cfg_parts = cfg.get("MAX_FORM_PARTS", max_parts)
                    if cfg_parts is not None:
                        max_parts = cfg_parts
                    cfg_part_size = cfg.get("MAX_FORM_PART_SIZE", max_part_size)
                    if cfg_part_size is not None:
                        max_part_size = cfg_part_size
                multipart_body = await self._drain_body()
                self._form = parse_multipart_form(
                    multipart_body,
                    self.content_type,
                    max_parts=max_parts,
                    max_part_size=max_part_size,
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

    async def is_disconnected(self) -> bool:
        """Whether the client has disconnected.

        Veloce fully buffers the request body before dispatch, so by
        the time a handler runs the body is already received and the
        connection cannot be "disconnected mid-handler" in the ASGI
        sense. Always returns `False`; the method exists so handlers that
        poll `await request.is_disconnected()` keep working unchanged.
        """
        return False

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
