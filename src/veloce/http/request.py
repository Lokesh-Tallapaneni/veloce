"""Request object — lazy parsing for speed."""

from __future__ import annotations

import asyncio
from typing import Any, NamedTuple

import orjson

from veloce.http.datastructures import (
    AcceptHeader,
    Authorization,
    Cookies,
    Headers,
    QueryParams,
    RangeSpec,
)


class Address(NamedTuple):
    """Client/server address — ASGI shape.

    A two-field named tuple so `request.client.host` /
    `request.client.port` work, while `host, port = request.client`
    unpacking also works (tuple semantics).
    """

    host: str
    port: int


class State(dict):
    """Per-request scratch namespace — supports both styles.

    ASGI servers expose `request.state` for attribute-style
    storage (`request.state.user = ...`). Veloce's dispatcher also
    stashes framework internals (`session`, `url_rule`, …) here by
    key. `State` is a `dict` subclass whose attribute access maps to
    items, so `state.user` and `state["user"]` / `state.get("user")`
    are interchangeable — neither call site needs to know the other.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError:
            raise AttributeError(name) from None


class Request:
    """Incoming HTTP request with lazy attribute parsing.

    All expensive operations (JSON parsing, cookie parsing, URL construction,
    form/multipart parsing) are deferred until accessed — zero overhead for
    properties you don't use.
    """

    __slots__ = (
        "method",
        "path",
        "query_string",
        "headers",
        "body",
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
    ) -> None:
        self.method = method.upper()
        self.path = path
        self.query_string = query_string
        # Always normalise into a Headers (CIMultiDict) so case-insensitive,
        # multi-value access works no matter how the caller passed headers in.
        if isinstance(headers, Headers):
            self.headers: Headers = headers
        else:
            self.headers = Headers(headers)  # type: ignore[arg-type]
        self.body = body
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

    @property
    def query_params(self) -> QueryParams:
        """Parse query string lazily — only when accessed.

        Repeated keys are preserved: `params.getlist("tag")` returns every
        value; `params["tag"]` returns the first.
        """
        if self._query_params is None:
            self._query_params = QueryParams.from_query_string(self.query_string)
        return self._query_params

    @property
    def args(self) -> QueryParams:
        """an alias for `query_params` — the parsed URL query string."""
        return self.query_params

    @property
    def data(self) -> bytes:
        """Raw request body bytes — `request.data` shape.

        The sync-property form of `get_data()`. Returns the body
        exactly as received, with no decoding or form parsing.
        """
        return self.body

    @property
    def path_params(self) -> dict[str, str]:
        return self._path_params

    @path_params.setter
    def path_params(self, value: dict[str, str]) -> None:
        self._path_params = value

    @property
    def view_args(self) -> dict[str, Any]:
        """an alias for `path_params` — the matched route's path params.

        Veloce names the dict of URL-captured values `request.view_args`;
        veloce calls it `path_params`. Both point at the same dict.
        """
        return self._path_params

    def json(self) -> Any:
        """Parse JSON body using orjson (3-10x faster than stdlib json)."""
        if self._json is None:
            self._json = orjson.loads(self.body) if self.body else None
        return self._json

    def get_data(self, as_text: bool = False, cache: bool = True) -> bytes | str:
        """Return the raw request body.

        - `as_text=True` decodes via the `Content-Type` charset (default
          UTF-8). Falls back to `latin-1` when the declared charset is
          unrecognised — a defensive fallback, since
          latin-1 round-trips arbitrary bytes without raising.
        - `cache=True` is a no-op today (veloce already buffers the
          whole body on construction) but keeps the parameter so
          callers that pass `cache=False` for streaming compatibility
          don't break. Streaming-body support arrives separately.

        Returns `bytes` (default) or `str` (with `as_text=True`).
        """
        _ = cache  # reserved for streaming-body work; no caching needed yet
        if not as_text:
            return self.body
        charset = self.mimetype_params.get("charset", "utf-8")
        try:
            return self.body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            return self.body.decode("latin-1")

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
        """
        if not force and not self.is_json:
            return None
        if not self.body:
            return None

        if cache and self._json is not None:
            return self._json

        try:
            parsed = orjson.loads(self.body)
        except orjson.JSONDecodeError as exc:
            if silent:
                return None
            return self.on_json_loading_failed(exc)

        if cache:
            self._json = parsed
        return parsed

    def on_json_loading_failed(self, error: Exception) -> Any:
        """Hook invoked when `get_json()` fails to parse a non-silent body.

        Override on a `Request` subclass to customise the
        failure behaviour (e.g. raise a `BadRequest` with a friendly
        message, or return a sentinel). The default re-raises the
        original decode error so malformed JSON surfaces loudly.
        """
        raise error

    async def form(self) -> Any:
        """Parse form data including file uploads."""
        if self._form is None:
            from urllib.parse import parse_qsl

            content_type = self.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in content_type:
                from veloce.http.datastructures import FormData

                # `parse_qsl` preserves duplicate keys as separate `(k, v)`
                # tuples; `MultiDict` ingests them via the iterable
                # constructor without collapsing.
                items = parse_qsl(self.body.decode("utf-8"), keep_blank_values=True)
                self._form = FormData(items)
            elif "multipart/form-data" in content_type:
                from veloce.http.datastructures import parse_multipart_form

                self._form = parse_multipart_form(self.body, content_type)
            else:
                from veloce.http.datastructures import FormData

                self._form = FormData()
        return self._form

    async def files(self) -> Any:
        """View of uploaded files only — a `FormData` subset.

        Parses the form (via `form()`) and returns a `FormData`
        containing just the entries whose value is an `UploadFile`.
        Non-file form fields are excluded. Empty `FormData` for
        non-multipart requests.
        """
        from veloce.http.datastructures import FormData, UploadFile

        form = await self.form()
        files = FormData()
        for key in form:
            for value in form.getlist(key):
                if isinstance(value, UploadFile):
                    files.add(key, value)
        return files

    @property
    def cookies(self) -> Cookies:
        """Parse cookies from the `Cookie` header — lazy, MultiDict-shaped.

        Returns a `Cookies` (MultiDict). `cookies["name"]` gives the first
        value; `cookies.getlist("name")` gives every value when a name
        repeats (rare but valid per RFC 6265).
        """
        if self._cookies is None:
            self._cookies = Cookies.from_cookie_header(self.headers.get("cookie", ""))
        return self._cookies

    @property
    def url(self) -> Any:
        """Full URL object — lazy construction."""
        if self._url is None:
            from veloce.http.datastructures import URL

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
        """Alias for `url_root` — Veloce exposes both names."""
        return self.url_root

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    @property
    def mimetype(self) -> str:
        """`Content-Type` without parameters.

        `application/json; charset=utf-8` → `application/json`. Lower-cased
        and stripped — per RFC 9110 §8.3 the media type is case-insensitive.
        """
        ct = self.content_type
        return ct.split(";", 1)[0].strip().lower() if ct else ""

    @property
    def mimetype_params(self) -> dict[str, str]:
        """Parameters from `Content-Type` (e.g. `{"charset": "utf-8"}`).

        Each parameter is `key=value`; quoted values have their surrounding
        double-quotes stripped. Keys are lower-cased; values preserve case.
        """
        ct = self.content_type
        if not ct or ";" not in ct:
            return {}
        result: dict[str, str] = {}
        for chunk in ct.split(";")[1:]:
            chunk = chunk.strip()
            if "=" not in chunk:
                continue
            k, _, v = chunk.partition("=")
            v = v.strip()
            if v.startswith('"') and v.endswith('"') and len(v) >= 2:
                v = v[1:-1]
            result[k.strip().lower()] = v
        return result

    @property
    def content_length(self) -> int | None:
        cl = self.headers.get("content-length")
        return int(cl) if cl else None

    @property
    def client_host(self) -> str | None:
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
        Honours ProxyFix — `client.host` reflects the trusted client IP.
        """
        host = self.client_host
        if host is None:
            # ASGI scope path — `scope["client"]` is `(host, port)`.
            scope_client = self.scope.get("client") if self.scope else None
            if scope_client:
                return Address(scope_client[0], scope_client[1])
            return None
        port = self.client_port or 0
        return Address(host, port)

    def url_for(self, name: str, **path_params: Any) -> str:
        """Reverse-resolve a route URL — ASGI shape.

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

    @property
    def is_secure(self) -> bool:
        return self.url.scheme == "https"

    async def values(self) -> Any:
        """Merged query string + form body — `request.values` shape.

        Returns a fresh `MultiDict` with query-string entries first,
        then form-body entries appended. Both source `MultiDict`s
        preserve repeated keys; merging preserves the order across
        both sources. Form parsing is async (multipart may need
        executor reads), so this is an awaitable rather than a property.
        """
        from multidict import MultiDict

        merged: MultiDict = MultiDict()
        for k, v in self.query_params.items():
            merged.add(k, v)
        form = await self.form()
        if form is not None:
            for k, v in form.items():
                merged.add(k, v)
        return merged

    @property
    def is_xhr(self) -> bool:
        """Detect XMLHttpRequest-style AJAX calls.

        The convention is `X-Requested-With: XMLHttpRequest`, set by
        jQuery, fetch wrappers, and similar libraries. It's a hint, not
        a guarantee (the client controls the header), but it's the
        traditional signal application code uses to switch between full
        HTML responses and partial / JSON ones.
        """
        return self.headers.get("x-requested-with", "").lower() == "xmlhttprequest"

    @property
    def scheme(self) -> str:
        """Request scheme — `"http"` or `"https"`.

        Sourced from the ASGI `scope["scheme"]` when present, then from
        the `X-Forwarded-Proto` header (only meaningful behind a trusted
        proxy), then default `http`.
        """
        return self.url.scheme

    @property
    def accept(self) -> str:
        return self.headers.get("accept", "")

    @property
    def max_content_length(self) -> int | None:
        """The body-size cap for this request.

        Reads `app.config["MAX_CONTENT_LENGTH"]` from the bound app
        (the dispatcher enforces it, returning 413 on overflow).
        `None` — no limit — when unset or no app is bound.
        """
        if self.app is None:
            return None
        cfg = getattr(self.app, "config", None)
        return cfg.get("MAX_CONTENT_LENGTH") if cfg is not None else None

    @property
    def origin(self) -> str | None:
        """The `Origin` header — RFC 6454. `None` when absent.

        Set by browsers on cross-origin requests (and all CORS
        preflights). CORS middleware matches the allow-list against it.
        """
        return self.headers.get("origin")

    @property
    def access_control_request_method(self) -> str | None:
        """CORS preflight `Access-Control-Request-Method` — RFC 6454.

        On an `OPTIONS` preflight, names the method the real request
        will use. `None` outside a preflight.
        """
        return self.headers.get("access-control-request-method")

    @property
    def access_control_request_headers(self) -> list[str]:
        """CORS preflight `Access-Control-Request-Headers` — header list.

        The headers the real request intends to send, lower-cased and
        whitespace-trimmed. Empty list when the header is absent.
        """
        raw = self.headers.get("access-control-request-headers", "")
        return [h.strip().lower() for h in raw.split(",") if h.strip()]

    @property
    def accept_mimetypes(self) -> AcceptHeader:
        """Parsed `Accept` header with MIME wildcard matching."""
        return AcceptHeader.parse(self.headers.get("accept", ""), mime=True)

    @property
    def accept_languages(self) -> AcceptHeader:
        """Parsed `Accept-Language` header. q-value ordered."""
        return AcceptHeader.parse(self.headers.get("accept-language", ""))

    @property
    def accept_encodings(self) -> AcceptHeader:
        """Parsed `Accept-Encoding` header (e.g. gzip, br)."""
        return AcceptHeader.parse(self.headers.get("accept-encoding", ""))

    @property
    def accept_charsets(self) -> AcceptHeader:
        """Parsed `Accept-Charset` header."""
        return AcceptHeader.parse(self.headers.get("accept-charset", ""))

    @property
    def authorization(self) -> str | None:
        return self.headers.get("authorization")

    @property
    def date(self) -> Any:
        """The request `Date` header as a tz-aware UTC `datetime`.

        RFC 9110 §6.6.1 — the originator's timestamp for the message.
        Returns `None` when the header is missing or unparseable.
        """
        from veloce.http.dates import parse_date

        return parse_date(self.headers.get("date"))

    @property
    def if_modified_since(self) -> float | None:
        """Parse `If-Modified-Since` (RFC 9110 §13.1.3) to a Unix timestamp.

        Accepts IMF-fixdate, obsolete RFC 850, and ANSI C `asctime()`
        forms. Returns `None` when the header is missing or unparseable
        — never raises, so callers can use it in a single branch.
        """
        raw = self.headers.get("if-modified-since", "")
        if not raw:
            return None
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(raw.strip())
        except (TypeError, ValueError):
            return None
        if dt is None:
            return None
        return dt.timestamp()

    @property
    def range(self) -> RangeSpec | None:
        """Parse `Range:` header per RFC 9110 §14.2. Returns `None` when
        absent or unparseable."""
        return RangeSpec.parse(self.headers.get("range", ""))

    @property
    def if_match(self) -> tuple[str, ...]:
        """Parse `If-Match` (RFC 9110 §13.1.1) into a tuple of ETags.

        Returns `("*",)` for the wildcard, an empty tuple when the
        header is absent, otherwise a tuple of quoted ETags (quotes
        and any `W/` weak prefix preserved verbatim).

        `If-Match` is the write-side companion to `If-None-Match`:
        precondition that fails the request with `412 Precondition
        Failed` when none of the listed ETags matches the resource's
        current ETag. Standard guard against the lost-update problem.
        """
        raw = self.headers.get("if-match", "")
        if not raw:
            return ()
        stripped = raw.strip()
        if stripped == "*":
            return ("*",)
        return tuple(t.strip() for t in stripped.split(",") if t.strip())

    @property
    def if_range(self) -> tuple[str, float | None]:
        """Parse `If-Range:` (RFC 9110 §13.1.5).

        The header carries **either** an ETag **or** an HTTP-date — never
        both. Returns `(etag, None)` when the value is an ETag (quoted,
        possibly weak-prefixed) and `("", timestamp)` when it parses as
        a date. Returns `("", None)` when the header is absent or
        unparseable. Caller picks the relevant slot.

        Used by `GET` with `Range:` to convert a partial-content request
        into a full `200` when the cached resource is stale.
        """
        raw = self.headers.get("if-range", "")
        if not raw:
            return ("", None)
        stripped = raw.strip()
        # ETag values are quoted (optionally `W/"..."`). Anything else
        # is interpreted as an HTTP-date.
        if stripped.startswith('"') or stripped.startswith('W/"'):
            return (stripped, None)
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(stripped)
        except (TypeError, ValueError):
            return ("", None)
        if dt is None:
            return ("", None)
        return ("", dt.timestamp())

    @property
    def if_unmodified_since(self) -> float | None:
        """Parse `If-Unmodified-Since` (RFC 9110 §13.1.4) to a Unix timestamp.

        Returns `None` when the header is missing or unparseable.
        Write-side companion to `If-Modified-Since`: precondition that
        fails with `412` when the resource has been modified since the
        given date.
        """
        raw = self.headers.get("if-unmodified-since", "")
        if not raw:
            return None
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(raw.strip())
        except (TypeError, ValueError):
            return None
        if dt is None:
            return None
        return dt.timestamp()

    @property
    def if_none_match(self) -> tuple[str, ...]:
        """Parse `If-None-Match` (RFC 9110 §13.1.4) into a tuple of ETags.

        Returns `("*",)` when the header is the literal `*` (matches any
        existing representation), an empty tuple when the header is
        missing, or a tuple of one or more quoted ETags (the quotes are
        preserved so callers can compare them verbatim against an ETag
        header on the response).
        """
        raw = self.headers.get("if-none-match", "")
        if not raw:
            return ()
        stripped = raw.strip()
        if stripped == "*":
            return ("*",)
        # Comma-separated, optionally with weak `W/` prefixes.
        return tuple(t.strip() for t in stripped.split(",") if t.strip())

    @property
    def auth(self) -> Authorization | None:
        """Lazy-parse the `Authorization:` header into a typed object.

        Returns `None` when the header is missing. `Basic` and `Bearer`
        schemes populate `.username/.password` and `.token` respectively;
        other schemes carry their key=value parameters in `.params`.
        """
        return Authorization.from_header(self.headers.get("authorization", ""))

    @property
    def user_agent(self) -> str:
        return self.headers.get("user-agent", "")

    @property
    def referrer(self) -> str:
        """Value of the `Referer` request header.

        Spelling preserved from the original RFC misprint (RFC 7231 §5.5.2
        documents `Referer`, not `Referrer`). The accessor uses the
        corrected spelling so callers don't have to remember.
        """
        return self.headers.get("referer", "")

    @property
    def root_path(self) -> str:
        """ASGI `scope["root_path"]` — the URL prefix the app is mounted under.

        Comes from the ASGI server (e.g. uvicorn `--root-path /api`) or
        from `app.mount("/sub", inner_app)`. Used so an app behind a
        prefix can generate correct external URLs without knowing the
        prefix at code-time.

        Returns the empty string when the app is at root.
        """
        return self.scope.get("root_path", "") if isinstance(self.scope, dict) else ""

    @property
    def script_root(self) -> str:
        """an alias for `root_path` — also called `script_root`.

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
        # No SERVER_NAME — return the leftmost label only when the host
        # has more than one label (otherwise it's the apex).
        if "." not in host:
            return ""
        return host.split(".", 1)[0]

    @property
    def is_multipart(self) -> bool:
        """`True` when the request body is `multipart/*`."""
        return self.mimetype.startswith("multipart/")

    @property
    def is_form(self) -> bool:
        """`True` when the body is `application/x-www-form-urlencoded`
        or `multipart/form-data`."""
        m = self.mimetype
        return m == "application/x-www-form-urlencoded" or m.startswith("multipart/")

    @property
    def content_encoding(self) -> str:
        """Value of the `Content-Encoding` header.

        Returns the lowercased encoding name (`"gzip"`, `"br"`, etc.)
        or the empty string when the header is missing.
        """
        return (self.headers.get("content-encoding", "") or "").strip().lower()

    @property
    def content_language(self) -> str:
        """Value of the `Content-Language` header — RFC 9110 §8.5.

        Returns the raw header value (a comma-separated list of
        language tags) or the empty string when the header is absent.
        """
        return self.headers.get("content-language", "") or ""

    @property
    def pragma(self) -> str:
        """Value of the legacy `Pragma` header — RFC 9111 §5.4.

        Almost always `no-cache` from HTTP/1.0 clients. Returns the
        empty string when absent. Prefer `cache_control` for HTTP/1.1.
        """
        return (self.headers.get("pragma", "") or "").strip().lower()

    @property
    def max_forwards(self) -> int | None:
        """The `Max-Forwards` header as an int — RFC 9110 §7.6.2.

        Bounds how many proxies a TRACE/OPTIONS request may traverse.
        `None` when absent or non-numeric.
        """
        raw = (self.headers.get("max-forwards", "") or "").strip()
        return int(raw) if raw.isdigit() else None

    @property
    def environ(self) -> dict:
        """Alias for the ASGI `scope` dict.

        third-party code paths reach for `request.environ` (WSGI); ASGI
        scope is the analogue. Returns the live dict so middleware can
        introspect (mutation goes through framework APIs, not this).
        """
        return self.scope if isinstance(self.scope, dict) else {}

    @property
    def host(self) -> str:
        """Value of the `Host` request header.

        Mirrors `Request.url.netloc` for the common case but pulls
        directly from the header to remain cheap (no full URL parse).
        Returns the empty string when the header is absent.
        """
        return self.headers.get("host", "")

    @property
    def remote_addr(self) -> str | None:
        """an alias for `client_host` — the connecting client's IP.

        Honours ProxyFix-style middleware: when the trusted hop has set
        `_state["proxy_fix_client"]`, that value wins over the raw TCP
        peer (the ASGI/uvicorn `client[0]` may be the load balancer).
        """
        return self.client_host

    @property
    def access_route(self) -> list[str]:
        """Forwarded-for chain.

        Returns the comma-separated `X-Forwarded-For` values (client →
        proxy chain order), with the connecting peer (`remote_addr`)
        appended at the end. With no `X-Forwarded-For` header, returns
        `[remote_addr]` when the peer is known, else `[]`.

        RFC 7239 §5.2 defines the IP-order convention: leftmost is the
        originating client, rightmost is the closest proxy. Production
        code should consume the leftmost *trusted* entry, not blindly
        the leftmost value.
        """
        forwarded = self.headers.get("x-forwarded-for", "")
        chain: list[str] = [v.strip() for v in forwarded.split(",") if v.strip()]
        peer = self.client_host
        if not peer:
            # ASGI scope path — `scope["client"]` is `(host, port)` per spec.
            client = self.scope.get("client") if self.scope else None
            if client:
                peer = client[0]
        if peer:
            chain.append(peer)
        return chain

    @property
    def charset(self) -> str:
        """Request body charset, decoded from `Content-Type`.

        Defaults to `utf-8` when no charset is declared (the modern
        default; the also moved off ISO-8859-1).
        """
        return self.mimetype_params.get("charset", "utf-8")

    @property
    def state(self) -> State:
        """Per-request scratch namespace — ASGI shape.

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
        run — keeps "I forgot to add SessionMiddleware" from showing up
        as a confusing silent empty-dict.
        """
        if "session" not in self._state:
            raise RuntimeError(
                "Request.session is unavailable — install SessionMiddleware "
                "(`app.add_middleware(SessionMiddleware(secret_key=...))`) "
                "to enable session storage."
            )
        return self._state["session"]

    @property
    def url_rule(self) -> str | None:
        """the matched route's template (e.g. `/users/{id}`).

        Returns the raw path template the radix tree used for the match —
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

    @property
    def cache_control(self) -> Any:
        """Parsed `Cache-Control` header.

        Returns a `CacheControl` view: `req.cache_control.no_cache`
        (bool), `req.cache_control.max_age` (int or None), etc.
        Always returns a fresh parse to reflect any header mutation.
        """
        from veloce.http.cache_control import CacheControl

        return CacheControl(self.headers.get("cache-control", ""))

    @property
    def is_json(self) -> bool:
        """True for `application/json` or any `application/*+json` subtype.

        Per RFC 6839 §3.1 the structured-suffix `+json` (e.g.
        `application/vnd.api+json`, `application/problem+json`) marks the
        body as JSON-encoded.
        """
        ct = self.content_type.lower().split(";", 1)[0].strip()
        if ct == "application/json":
            return True
        return ct.startswith("application/") and ct.endswith("+json")

    async def text(self) -> str:
        """Return body as text."""
        return self.body.decode("utf-8", errors="replace")

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
        """Async-iterate the request body — ASGI shape.

        Veloce buffers the whole body before dispatch, so this yields
        the entire body as a single chunk (then terminates). It exists
        so handlers written against the streaming API
        (`async for chunk in request.stream(): ...`) keep working unchanged.
        """
        if self.body:
            yield self.body
