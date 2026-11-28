"""Request core — slotted state, method and path, headers and body metadata."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from typing_extensions import Self

from veloce._constants import (
    HEADER_CONTENT_ENCODING,
    HEADER_CONTENT_LANGUAGE,
    HEADER_CONTENT_LENGTH,
    HEADER_CONTENT_TYPE,
    HEADER_DATE,
    HEADER_MAX_FORWARDS,
    HEADER_ORIGIN,
    HEADER_PRAGMA,
    HEADER_REFERER,
    HEADER_USER_AGENT,
    HEADER_X_REQUESTED_WITH,
    MIME_FORM_URLENCODED,
)
from veloce._header_parsing import parse_media_type_params
from veloce._internal import (
    is_json_mimetype,
)
from veloce.http.datastructures import (
    AcceptHeader,
    Cookies,
    FormData,
    Headers,
    QueryParams,
    State,
)
from veloce.http.dates import parse_date

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime


# "Not read yet", where the parsed value may itself legitimately be `None`.
_UNSET: Any = object()

# "Not parsed yet", so a body that is JSON `null` still caches as `None`.
_UNPARSED: Any = object()


if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.request.request import Request


class RequestBase:
    """State and the header-level accessors every request shares."""

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
        "_subdomain",
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
        "_receive",
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
        scope: dict[str, Any] | None = None,
        body_source: Any = None,
        receive: Callable[[], Any] | None = None,
    ) -> None:
        # Both ASGI and `add_route` feed uppercase, so skip the allocation.
        self.method = method if method.isupper() else method.upper()
        self.path = path
        self.query_string = query_string
        # Deferred until a handler reads `request.headers`: the hot json-hello
        # path never touches them, and eager construction cost 2-3 us per request.
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
            self._headers = Headers(headers)  # type: ignore[arg-type]  # headers is normalised to the accepted shapes upstream
        # Either `body` already holds every byte, or `body_source` is fed by the
        # protocol; `_body` caches the assembled bytes for the sync accessors.
        self._body_source: Any = body_source
        if body_source is None:
            self._body: bytes = body
            self._body_drained: bool = True
        else:
            self._body = body
            self._body_drained = False
        # For `is_disconnected()`: a buffered route has no source to read it from.
        self._receive = receive
        # Set by a transport that already applied this app's MAX_CONTENT_LENGTH;
        # False for a mounted sub-app's request, checked against its own limit.
        self._length_enforced = False
        self.transport = transport
        self.app = app
        self.scope = scope or {}
        # The matched route's name; `None` outside dispatch.
        self.endpoint: str | None = None
        self._json: Any = _UNPARSED
        self._query_params: QueryParams | None = None
        self._form: Any = None
        self._path_params: dict[str, Any] = {}
        self._state: State = State()
        self._cookies: Cookies | None = None
        self._url: Any = None
        self._background_tasks: Any = None
        # Lazily-parsed `(mimetype, params)` for `Content-Type`.
        self._parsed_ct: tuple[str, dict[str, str]] | None = None
        self._subdomain: str | None = None
        self._accept_mimetypes: AcceptHeader | None = None
        self._accept_languages: AcceptHeader | None = None
        self._accept_encodings: AcceptHeader | None = None
        self._accept_charsets: AcceptHeader | None = None
        self._files: FormData | None = None
        # Conditional-header caches: `_UNSET` is the miss for the date-shaped
        # properties, `None` for the tuple-shaped ones.
        self._access_control_request_headers: list[str] | None = None
        self._if_modified_since: Any = _UNSET
        self._if_unmodified_since: Any = _UNSET
        self._if_match: tuple[str, ...] | None = None
        self._if_none_match: tuple[str, ...] | None = None
        self._if_range: tuple[str, float | None] | None = None
        self._range: Any = _UNSET
        self._auth: Any = _UNSET

    @classmethod
    def derive_for_mount(
        cls,
        parent: Request,
        sub_path: str,
        body: bytes,
        sub_app: Any,
        prefix: str,
        body_source: Any = None,
    ) -> Self:
        """Build the request a mounted sub-app is dispatched with.

        The sub-app answers the same connection, so everything the connection
        determined - the scheme, the client address, the server, any ASGI
        extensions - is carried forward rather than re-declared. Synthesising a
        scope holding only `root_path` is what left `request.is_secure` reading
        `False` over TLS and `request.client_host` reading `None` inside a mount,
        and would have dropped every scope-derived property added later too.

        Only what the mount itself changes is overridden: the path the sub-app
        sees, the `root_path` that makes its `url_for` build prefix-correct URLs,
        and the now-stale absolute `raw_path`. This mirrors what `_asgi_app`
        already does for a mounted raw ASGI app.

        `body_source` hands the live body over instead of bytes, for a sub-app
        route that streams: only the sub-app knows whether its route does, so
        the transport cannot decide before dispatch reaches here.

        `_length_enforced` deliberately stays `False`: the sub-app is checked
        against its own `MAX_CONTENT_LENGTH`, not its parent's.
        """
        scope = dict(parent.scope)
        scope["path"] = sub_path
        scope["root_path"] = parent.root_path + prefix
        scope.pop("raw_path", None)
        return cls(
            method=parent.method,
            path=sub_path,
            query_string=parent.query_string,
            headers=parent.headers,
            body=body,
            transport=parent.transport,
            app=sub_app,
            scope=scope,
            body_source=body_source,
            receive=parent._receive,
        )

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
            # Materialised from the raw tuples, which are then released.
            raw = self._headers_raw or ()
            h = Headers([(k.decode("latin-1"), v.decode("latin-1")) for k, v in raw])
            self._headers = h
            self._headers_raw = None
        return h

    @headers.setter
    def headers(self, value: Headers | dict[str, str] | list[tuple[str, str]]) -> None:
        self._headers = value if isinstance(value, Headers) else Headers(value)
        self._headers_raw = None

    def _peek_header(self, name: str) -> str | None:
        """Read one header without building the whole mapping.

        Materialising `headers` decodes every name and value and builds a
        `CIMultiDict` - measured at ~2.7 us for an eight-header request, of
        which ~1.8 us is the latin-1 decoding. A route reading a single header
        paid all of it to look at one value.

        Prefer `_peek_header_key` where the name is fixed at registration:
        lowercasing and encoding it here costs ~200 ns that a caller holding
        the bytes key already avoids.
        """
        return self._peek_header_key(name.lower().encode("latin-1"))

    def _peek_header_key(self, key: bytes) -> str | None:
        """`_peek_header` for a caller that already holds the lowercase key.

        Matches the case-insensitive semantics of the `Headers` mapping, so the
        two never disagree about which value a repeated name resolves to, and
        answers without decoding the rest of the block. An absent header costs
        one scan and no mapping build, which is the shape every optional
        `Header()` and every unauthenticated request takes.
        """
        h = self._headers
        if h is not None:
            return h.get(key.decode("latin-1"))
        for name, value in self._headers_raw or ():
            # Inline, not a fallback: both spellings present, a fallback would
            # authenticate the one no reader of `.headers` sees.
            if name == key or name.lower() == key:
                return value.decode("latin-1")
        return None

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
            # `partition` already said there are none, as most types have.
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
            # Read per request by the body-size guard, so it scans the raw tuples
            # rather than defeating `__init__`'s header deferral.
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

        Defaults to `utf-8` when no charset is declared, which is what the
        HTTP specifications settled on after moving off ISO-8859-1.
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
        """`True` when the body is form-encoded or multipart.

        That is, `application/x-www-form-urlencoded` or `multipart/form-data`.
        """
        m = self.mimetype
        return m == MIME_FORM_URLENCODED or m.startswith("multipart/")
