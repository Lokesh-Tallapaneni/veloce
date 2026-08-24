"""Test client — in-memory driver for the app's ASGI surface.

Constructs ASGI scopes directly and calls `app.__call__(scope, receive, send)`
on a dedicated event loop. This exercises the same path a production ASGI
server would take - the radix router, dependency resolver, middleware chain,
response encoder, and ASGI lifespan handshake.

The external API exposes both an `app.test_client()` factory and a
`TestClient` class: `client.get(...)`, `client.post(json=...)`,
`response.status_code`, `response.json()`, `response.text`, `response.headers`,
`response.content_type`, plus cookie persistence across calls.

The ASGI scope shape, message names, and header byte-list layout follow
the ASGI specification.
"""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import secrets
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import unquote, urlencode, urlparse

import orjson
from multidict import CIMultiDict

from veloce._constants import (
    HEADER_CONTENT_TYPE,
    HEADER_LOCATION,
    HEADER_SET_COOKIE,
    MIME_FORM_URLENCODED,
    MIME_JSON,
    MIME_MULTIPART_FORM_DATA,
    MIME_OCTET_STREAM,
)
from veloce._protocol_constants import (
    ASGI_EVENT_HTTP_DISCONNECT,
    ASGI_EVENT_HTTP_REQUEST,
    ASGI_EVENT_HTTP_RESPONSE_BODY,
    ASGI_EVENT_HTTP_RESPONSE_START,
    ASGI_EVENT_WS_ACCEPT,
    ASGI_EVENT_WS_CLOSE,
    ASGI_EVENT_WS_CONNECT,
    ASGI_EVENT_WS_DISCONNECT,
    ASGI_EVENT_WS_RECEIVE,
    ASGI_SCOPE_HTTP,
    ASGI_SCOPE_WEBSOCKET,
    HTTP_METHOD_DELETE,
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
    HTTP_METHOD_PATCH,
    HTTP_METHOD_POST,
    HTTP_METHOD_PUT,
    LIFECYCLE_SHUTDOWN,
    LIFECYCLE_STARTUP,
    SET_COOKIE_JOINER,
    URL_SCHEME_HTTP,
    URL_SCHEME_WS,
)
from veloce.status import (
    HTTP_301_MOVED_PERMANENTLY,
    HTTP_302_FOUND,
    HTTP_303_SEE_OTHER,
    HTTP_307_TEMPORARY_REDIRECT,
    HTTP_308_PERMANENT_REDIRECT,
    HTTP_500_INTERNAL_SERVER_ERROR,
    WS_1000_NORMAL_CLOSURE,
)

# ── Shared module helpers ─────────────────────────────────

# Set-Cookie header-name match is case-insensitive; precompute the lowercased
# form once so the per-header scan in `TestResponse` and the cookie-jar update
# do not recompute it on every header.
_SET_COOKIE_LOWER = HEADER_SET_COOKIE.lower()


def _parse_set_cookie_first_pair(value: str) -> tuple[str, str] | None:
    """Return the `(name, value)` of a Set-Cookie header's first segment.

    The leading `name=value` pair is the cookie itself; the rest are
    attributes. Returns `None` when the first segment has no `=`.
    """
    first = value.split(";", 1)[0].strip()
    if "=" not in first:
        return None
    cname, _, cval = first.partition("=")
    return cname.strip(), cval.strip()


def _resolve_redirect_location(location: str, base_url: str) -> tuple[str, str]:
    """Decompose a `Location` header into `(path, query)` for the next hop.

    The test client has no live network: an absolute URL whose host is not the
    test client's own host cannot be followed. Same-host absolute URLs are
    reduced to `path?query#fragment`; relative locations pass through.
    """
    parsed = urlparse(location)
    if parsed.scheme and parsed.netloc:
        own_host = urlparse(base_url).netloc
        if parsed.netloc != own_host:
            raise RuntimeError(
                f"TestClient cannot follow redirect to absolute URL {location!r} "
                f"on a different host (test client host is {own_host!r})"
            )
        # Fragments are stripped by browsers on redirect - drop them.
        return parsed.path or "/", parsed.query
    # Relative location (or odd scheme-only) - use verbatim, splitting any qs.
    path, _, query = location.partition("?")
    return path, query


# Redirect status codes the test clients follow when `follow_redirects` is on.
_REDIRECT_STATUSES = frozenset(
    (
        HTTP_301_MOVED_PERMANENTLY,
        HTTP_302_FOUND,
        HTTP_303_SEE_OTHER,
        HTTP_307_TEMPORARY_REDIRECT,
        HTTP_308_PERMANENT_REDIRECT,
    )
)
# 301/302/303 rewrite the next hop to GET with an empty body; 307/308 preserve.
_REDIRECT_TO_GET = frozenset((HTTP_301_MOVED_PERMANENTLY, HTTP_302_FOUND, HTTP_303_SEE_OTHER))


async def _follow_redirects(
    *,
    build_headers: Callable[[dict[str, str] | None], dict[str, str]],
    send_one: Callable[[str, str, str, dict[str, str], bytes, Any | None], Awaitable[TestResponse]],
    update_cookies: Callable[[TestResponse], None],
    max_redirects: int,
    base_url: str,
    follow: bool,
    client_name: str,
    method: str,
    path: str,
    query_string: str,
    body: bytes,
    headers: dict[str, str] | None,
    stream: Any | None,
) -> TestResponse:
    """Drive the request/redirect loop shared by both test clients.

    `send_one` is the per-hop async dispatch (`_send_one_request`); the only
    difference between the sync and async clients is how that coroutine is
    awaited, which stays in each client's `_make_request`. The status
    classification, GET-rewrite, one-shot-stream replay guard, and hop cap
    live here so a redirect-semantics fix lands in one place.

    RFC 9110 Sec. 15.4: 303 always changes the method to GET and drops the
    body; 301/302 historically did the same (every browser does), and for
    test-client predictability the same browser convention is followed here.
    307/308 strictly preserve method and body.
    """
    current_method = method
    current_path = path
    current_query = query_string
    current_body = body
    current_headers = headers
    current_stream = stream

    for _ in range(max_redirects + 1):
        all_headers = build_headers(current_headers)
        resp = await send_one(
            current_method,
            current_path,
            current_query,
            all_headers,
            current_body,
            current_stream,
        )
        update_cookies(resp)
        if not follow or resp.status_code not in _REDIRECT_STATUSES:
            return resp
        location = resp.headers.get(HEADER_LOCATION)
        if not location:
            return resp
        if resp.status_code in _REDIRECT_TO_GET:
            current_method = HTTP_METHOD_GET
            current_body = b""
            # The body is dropped on this hop, so the one-shot stream iterator
            # is no longer needed (and must not be re-consumed).
            current_stream = None
        elif current_stream is not None:
            # 307/308 must replay method + body, but a consumed one-shot
            # iterator cannot be re-read. Fail loudly rather than silently
            # sending an empty body.
            raise RuntimeError(
                "streamed request body cannot be replayed across redirects; "
                "pass follow_redirects=False"
            )
        current_path, current_query = _resolve_redirect_location(location, base_url)
    raise RuntimeError(
        f"{client_name} exceeded {max_redirects} redirects following {method} {path}"
    )


async def _aiter_body_chunks(stream: Any) -> AsyncIterator[bytes]:
    """Normalise a user-supplied streaming body into an async iterator of bytes.

    Accepts a sync `Iterable[bytes | str]` or an `AsyncIterable[bytes | str]`;
    `str` chunks are UTF-8 encoded. Other element types raise `TypeError`. A
    plain `bytes` object is rejected here (it is not a stream) - callers pass
    raw bytes through `content=` instead.
    """
    if hasattr(stream, "__aiter__"):
        async for chunk in stream:
            yield chunk.encode("utf-8") if isinstance(chunk, str) else _coerce_chunk(chunk)
    elif hasattr(stream, "__iter__") and not isinstance(stream, (bytes, bytearray)):
        for chunk in stream:
            yield chunk.encode("utf-8") if isinstance(chunk, str) else _coerce_chunk(chunk)
    else:
        raise TypeError(
            "stream must be a sync Iterable or an AsyncIterable of bytes/str chunks, "
            f"got {type(stream).__name__}"
        )


def _coerce_chunk(chunk: Any) -> bytes:
    """Coerce a single non-str stream chunk to `bytes`, rejecting other types."""
    if isinstance(chunk, bytes):
        return chunk
    if isinstance(chunk, bytearray):
        return bytes(chunk)
    raise TypeError(f"stream chunk must be bytes or str, got {type(chunk).__name__}")


def _build_receive(body: bytes, stream: Any | None) -> Any:
    """Build the ASGI `receive` callable shared by both test clients.

    With no `stream`, a single `http.request` frame carries the whole `body`
    (the historical fast path). With a `stream`, each iterator chunk becomes a
    `more_body: True` frame, followed by a terminal empty `more_body: False`
    frame; reads past end-of-body return `http.disconnect`.
    """
    if stream is None:
        body_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if body_sent:
                # ASGI middleware that legitimately reads past end-of-body
                # (introspection, fan-out, replay) should see a clean
                # `http.disconnect` rather than hang forever on a never-set
                # Event.
                return {"type": ASGI_EVENT_HTTP_DISCONNECT}
            body_sent = True
            return {"type": ASGI_EVENT_HTTP_REQUEST, "body": body, "more_body": False}

        return receive

    chunks = _aiter_body_chunks(stream)
    exhausted = False
    disconnected = False

    async def stream_receive() -> dict[str, Any]:
        nonlocal exhausted, disconnected
        if disconnected:
            return {"type": ASGI_EVENT_HTTP_DISCONNECT}
        if exhausted:
            disconnected = True
            return {"type": ASGI_EVENT_HTTP_DISCONNECT}
        try:
            chunk = await chunks.__anext__()
        except StopAsyncIteration:
            exhausted = True
            return {"type": ASGI_EVENT_HTTP_REQUEST, "body": b"", "more_body": False}
        return {"type": ASGI_EVENT_HTTP_REQUEST, "body": chunk, "more_body": True}

    return stream_receive


class TestResponse:
    """View into a single ASGI response cycle."""

    __slots__ = (
        "status_code",
        "body",
        "headers",
        "content_type",
        "cookies",
        "raw_headers",
    )

    def __init__(
        self,
        status_code: int,
        body: bytes,
        raw_headers: list[tuple[bytes, bytes]],
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.raw_headers = raw_headers
        # Convert raw header list into a case-insensitive mapping so callers
        # that check `headers["Content-Type"]` work regardless of whether
        # the ASGI app lower-cased its keys (it should, per the spec).
        # Set-Cookie is multi-valued in the ASGI list, but we collapse to
        # the joined-by-`\r\nSet-Cookie: ` form for the headers mapping
        # for back-compat; the full per-cookie list lives in `raw_headers`.
        flat: CIMultiDict[str] = CIMultiDict()
        set_cookies: list[str] = []
        for k, v in raw_headers:
            name = k.decode("latin-1")
            value = v.decode("latin-1")
            if name.lower() == _SET_COOKIE_LOWER:
                set_cookies.append(value)
            else:
                flat[name] = value
        if set_cookies:
            flat[HEADER_SET_COOKIE] = SET_COOKIE_JOINER.join(set_cookies)
        self.headers = flat
        self.content_type = flat.get(HEADER_CONTENT_TYPE) or ""
        # Parse cookies from all Set-Cookie headers; each cookie's first
        # `name=value` segment wins.
        self.cookies: dict[str, str] = {}
        for line in set_cookies:
            pair = _parse_set_cookie_first_pair(line)
            if pair is not None:
                self.cookies[pair[0]] = pair[1]

    def json(self) -> Any:
        """Parse the response body as JSON and return the result."""
        return orjson.loads(self.body)

    @property
    def text(self) -> str:
        """Decode the response body as UTF-8 text."""
        return self.body.decode("utf-8", errors="replace")

    def __repr__(self) -> str:
        return f"<TestResponse [{self.status_code}]>"


def _build_request_headers(
    base: dict[str, str],
    cookies: dict[str, str],
    extra: dict[str, str] | None,
) -> dict[str, str]:
    """Merge the test client's persistent base headers + cookie jar + the
    per-call `extra` into one request-ready header dict. Single home for
    the merge order so the sync and async test clients stay in lockstep.
    """
    merged = dict(base)
    if cookies:
        merged["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    if extra:
        merged.update(extra)
    return merged


def _apply_set_cookie_to_jar(jar: dict[str, str], raw_headers: list[tuple[bytes, bytes]]) -> None:
    """Update `jar` from `Set-Cookie` response headers. Honours `Max-Age=0`
    as a deletion signal (RFC 6265 Sec. 5.2.2). Both test clients share this
    so a fix to the cookie semantics applies to sync + async at once.
    """
    for name_bytes, value_bytes in raw_headers:
        name = name_bytes.decode("latin-1")
        if name.lower() != _SET_COOKIE_LOWER:
            continue
        value = value_bytes.decode("latin-1")
        pair = _parse_set_cookie_first_pair(value)
        if pair is None:
            continue
        cname, cval = pair
        rest = value.split(";")[1:]
        is_deletion = any("max-age=0" in seg.strip().lower() for seg in rest)
        if is_deletion:
            jar.pop(cname, None)
        else:
            jar[cname] = cval


def _guess_content_type(filename: str | None, content: Any) -> str:
    """Pick a Content-Type for a multipart file part.

    Prefers the file-like object's `.type` attribute when present (matches
    Python's `cgi.FieldStorage` shape that some test helpers expose), then
    falls back to `mimetypes.guess_type(filename)`, finally to the
    `application/octet-stream` default the multipart spec recommends for
    unknown payloads.
    """
    explicit = getattr(content, "type", None)
    if explicit:
        return str(explicit)
    if filename:
        guess = mimetypes.guess_type(filename)[0]
        if guess:
            return guess
    return MIME_OCTET_STREAM


def _encode_multipart(
    files: dict[str, Any],
    fields: dict[str, str],
) -> tuple[bytes, str]:
    """Build a `multipart/form-data` body from files + extra form fields.

    `files` shape per RFC 7578 Sec. 4 - values can be:
    - `bytes` / `str`: raw file content, filename inferred from key.
    - file-like (`BytesIO`, open file handle, anything with `.read()`):
      content read on demand, filename from `getattr(spec, "name", key)`.
    - `(filename, content_or_filelike)`: 2-tuple.
    - `(filename, content_or_filelike, content_type)`: 3-tuple.

    For shapes that don't carry an explicit content-type, the helper
    guesses one from the filename extension and falls back to
    `application/octet-stream`. Mirrors `requests` / `httpx` shape so
    test code migrating from those clients works unchanged.

    `fields` are non-file form parts. Both files and fields appear in
    the body in registration order. Returns `(body, content_type)`
    where the content_type carries the random boundary.
    """

    def _q(value: str) -> str:
        # Per RFC 7578 Sec. 4.2 / RFC 2616 Sec. 2.2 quoted-string: escape `"` and
        # `\`; reject embedded CR or LF which cannot be carried inside a
        # quoted-string and would otherwise let a caller inject header
        # fields into the multipart preamble.
        if "\r" in value or "\n" in value:
            raise ValueError("multipart name / filename must not contain CR or LF")
        return value.replace("\\", "\\\\").replace('"', '\\"')

    boundary = "----veloce-" + secrets.token_hex(16)
    parts: list[bytes] = []
    b = boundary.encode("ascii")

    for name, value in fields.items():
        parts.append(b"--" + b + b"\r\n")
        parts.append(f'Content-Disposition: form-data; name="{_q(name)}"\r\n\r\n'.encode())
        parts.append(value.encode("utf-8") if isinstance(value, str) else value)
        parts.append(b"\r\n")

    for name, spec in files.items():
        # Match `requests` / `httpx`: accept bytes / str, file-likes (any
        # object with `.read()` - BytesIO, open file handle, IO[bytes]),
        # 2-tuple `(filename, content_or_filelike)`, or 3-tuple
        # `(filename, content_or_filelike, content_type)`. Tests
        # migrating from `requests.post(files={"f": BytesIO(...)})`
        # used to crash with a TypeError here.
        if isinstance(spec, (bytes, str)):
            filename, content, ct = name, spec, MIME_OCTET_STREAM
        elif isinstance(spec, tuple) and len(spec) == 2:
            filename, content = spec
            ct = _guess_content_type(filename, content)
        elif isinstance(spec, tuple) and len(spec) == 3:
            filename, content, ct = spec
        elif hasattr(spec, "read"):
            # Bare file-like - pull filename from `.name` when present
            # (open()'d files set it; BytesIO doesn't), fall back to
            # the field name.
            filename = getattr(spec, "name", None) or name
            content = spec
            ct = _guess_content_type(filename, content)
        else:
            raise TypeError(f"files[{name!r}] must be bytes, str, file-like, or 2/3-tuple")
        # Resolve the body bytes for any file-like content.
        if hasattr(content, "read"):
            content = content.read()
        if isinstance(content, str):
            content = content.encode("utf-8")
        parts.append(b"--" + b + b"\r\n")
        parts.append(
            (
                f'Content-Disposition: form-data; name="{_q(name)}"; '
                f'filename="{_q(filename)}"\r\n'
                f"Content-Type: {ct}\r\n\r\n"
            ).encode()
        )
        parts.append(content)
        parts.append(b"\r\n")

    parts.append(b"--" + b + b"--\r\n")
    body = b"".join(parts)
    return body, f"{MIME_MULTIPART_FORM_DATA}; boundary={boundary}"


async def _send_one_request(
    app: Any,
    method: str,
    path: str,
    query_string: str,
    headers: dict[str, str],
    body: bytes,
    stream: Any | None = None,
) -> TestResponse:
    """Drive one ASGI request through `app` and collect its response.

    Shared by both clients so a change to the scope or receive channel cannot
    reach one and miss the other.
    """
    scope = _build_scope(method, path, query_string, headers)
    receive = _build_receive(body, stream)
    return await _collect_response(app, scope, receive)


def _assemble_body(
    json: Any,
    data: dict[str, str] | None,
    content: bytes | None,
    files: dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> tuple[bytes, dict[str, str]]:
    """Encode one buffered request body and the content type it implies.

    Shared by both clients so the sync and async paths cannot drift on which
    argument wins or on the media type each form sets.
    """
    hdrs = dict(headers or {})
    body = b""
    if files is not None:
        body, ct = _encode_multipart(files, data or {})
        hdrs[HEADER_CONTENT_TYPE] = ct
    elif json is not None:
        body = orjson.dumps(json)
        hdrs.setdefault(HEADER_CONTENT_TYPE, MIME_JSON)
    elif data is not None:
        body = urlencode(data).encode()
        hdrs.setdefault(HEADER_CONTENT_TYPE, MIME_FORM_URLENCODED)
    elif content is not None:
        body = content
    return body, hdrs


def _build_scope(
    method: str,
    path: str,
    query_string: str,
    headers: dict[str, str],
    client: tuple[str, int] = ("testclient", 50000),
    server: tuple[str, int] = ("testserver", 80),
    scheme: str = URL_SCHEME_HTTP,
    root_path: str = "",
) -> dict[str, Any]:
    """Build an ASGI 3.0 HTTP scope. Header values are encoded latin-1 per spec."""
    raw_headers: list[tuple[bytes, bytes]] = []
    seen_host = False
    for k, v in headers.items():
        if k.lower() == "host":
            seen_host = True
        raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    if not seen_host:
        raw_headers.append((b"host", server[0].encode("latin-1")))

    return {
        "type": ASGI_SCOPE_HTTP,
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": scheme,
        # ASGI defines `path` as the target with percent-encoded sequences
        # decoded, which is what a real server hands the app - so the client
        # decodes too. Without it a decoding-dependent bug that appears under
        # uvicorn is invisible to the in-process suite.
        "path": unquote(path),
        # ASGI permits non-ASCII bytes in `raw_path` (UTF-8 is the only
        # universally-decodable encoding for percent-decoded paths). Plain
        # `path.encode("ascii")` would crash the moment a test reached a
        # non-ASCII URL. This one stays undecoded - it is the raw target.
        "raw_path": path.encode("utf-8"),
        "query_string": query_string.encode("ascii"),
        "root_path": root_path,
        "headers": raw_headers,
        "client": client,
        "server": server,
    }


async def _collect_response(app: Any, scope: dict[str, Any], receive: Any) -> TestResponse:
    """Drive one ASGI request/response cycle and assemble a `TestResponse`.

    Shared by the sync and async clients' `_send_one_request` so the `send`
    closure and body assembly live in one place (a fix to either applies to
    both, per the async/sync parity guardrail).
    """
    status_code = HTTP_500_INTERNAL_SERVER_ERROR
    raw_headers: list[tuple[bytes, bytes]] = []
    body_chunks: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code, raw_headers
        mtype = message["type"]
        if mtype == ASGI_EVENT_HTTP_RESPONSE_START:
            status_code = message["status"]
            raw_headers = list(message.get("headers") or [])
        elif mtype == ASGI_EVENT_HTTP_RESPONSE_BODY:
            chunk = message.get("body", b"")
            if chunk:
                body_chunks.append(chunk)
            # `more_body` False signals end of response.

    await app(scope, receive, send)
    return TestResponse(status_code, b"".join(body_chunks), raw_headers)


def _new_loop() -> asyncio.AbstractEventLoop:
    """Build the event loop a test client drives its app on.

    On Windows the default is the proactor loop, whose every iteration goes
    through an I/O completion port. The in-memory client opens no socket and
    spawns no process, so that machinery runs on each request and finds nothing
    to do; the selector loop does the same work materially cheaper.

    The one thing it cannot do is `asyncio.create_subprocess_*`, which on
    Windows requires the proactor loop. A test whose handler spawns a
    subprocess passes its own loop instead: `TestClient(app,
    loop=asyncio.ProactorEventLoop())`.
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


# ── Sync client ───────────────────────────────────────────


class TestClient:
    """Sync test client - drives the app through its ASGI surface.

    Usage::

        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"message": "ok"}
    """

    __test__ = False  # don't let pytest collect this as a test class

    # Hop cap for redirect chasing. Matches httpx's default. Loops or
    # ping-pong redirects raise rather than silently spin.
    _MAX_REDIRECTS = 10

    def __init__(
        self,
        app: Any,
        base_url: str = f"{URL_SCHEME_HTTP}://testserver",
        follow_redirects: bool = False,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.app = app
        self.base_url = base_url
        self.follow_redirects = follow_redirects
        self._cookies: dict[str, str] = {}
        self._base_headers: dict[str, str] = {}
        self._owns_loop = False
        self._lifespan_run = False

        if loop is not None:
            # A caller's loop is theirs to close; see `_new_loop` for the one
            # case that needs this.
            self._loop = loop
        else:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = _new_loop()
                self._owns_loop = True

        # The in-memory test client is a testing tool: relax the
        # first-request setup lock so a test can register routes/hooks between
        # calls (the lock guards real concurrent serving, not single-threaded
        # test dispatch). Remembered and restored on `close()`, because the app
        # is not the client's to change permanently: an app touched by a
        # TestClient and then served in the same process never froze its route
        # table again, whatever DEBUG or TESTING said.
        self._prior_setup_lock: bool | None = None
        if hasattr(app, "_setup_lock_enabled"):
            self._prior_setup_lock = app._setup_lock_enabled
            app._setup_lock_enabled = False

        # Run startup lifecycle once at construction so users can mutate
        # app.state etc. in startup hooks before the first call.
        if hasattr(app, "_run_lifecycle"):
            self._loop.run_until_complete(app._run_lifecycle(LIFECYCLE_STARTUP))
            self._lifespan_run = True

    # ── Cookie management (conventional shape) ────────────

    @property
    def cookies(self) -> _TestClientCookies:
        """Live view of the client's cookie jar.

        Supports dict-like access (`client.cookies["session"]`),
        assignment (`client.cookies["k"] = "v"`), deletion, iteration,
        and bulk `clear()`. Cookies the server sends on responses are
        automatically merged in via `_update_cookies`. The state
        persists across calls until the client is closed.
        """
        return _TestClientCookies(self)

    def set_cookie(self, key: str, value: str) -> None:
        """Add or update a cookie sent on every subsequent request."""
        self._cookies[key] = value

    def delete_cookie(self, key: str) -> None:
        """Remove a cookie from the jar. No-op if not present."""
        self._cookies.pop(key, None)

    @contextlib.contextmanager
    def session_transaction(self) -> Any:
        """Mutate the session outside a request.

        Yields a `Session` dict pre-loaded from the current session
        cookie (if any). On block exit the session is re-signed with the
        app's `SessionMiddleware` secret and stored in the cookie jar, so
        the next request carries it::

            with client.session_transaction() as sess:
                sess["user_id"] = 7

        Raises `RuntimeError` if the app has no `SessionMiddleware`.
        """
        from veloce.middleware.sessions import (
            SessionMiddleware,
            SessionMiddlewareBase,
            _build_signer,
        )
        from veloce.sessions import Session
        from veloce.signing import BadSignature

        registered = getattr(self.app, "_middlewares", [])
        mw = next((m for m in registered if isinstance(m, SessionMiddleware)), None)
        if mw is None:
            # Distinguish "no session at all" from "a session this cannot seed":
            # the second is a real setup, and the generic message sent the reader
            # looking for a mistake they had not made.
            other = next((m for m in registered if isinstance(m, SessionMiddlewareBase)), None)
            if other is not None:
                raise RuntimeError(
                    f"session_transaction() seeds a signed cookie, which "
                    f"{type(other).__name__} does not use - write to its store directly instead"
                )
            raise RuntimeError("session_transaction() requires SessionMiddleware on the app")

        # The signing key may still be waiting on `SECRET_KEY`: it is resolved on
        # the first request, and seeding a session before one is exactly what
        # this helper is for. Settle it now rather than failing on a missing
        # attribute.
        if mw._pending_config:
            secret = self.app.config.get("SECRET_KEY")
            if not secret:
                raise RuntimeError(
                    "session_transaction() needs a signing key - pass secret_key= to the "
                    "middleware, or set app.secret_key"
                )
            mw._signer = _build_signer(secret)
            mw._pending_config = False

        # The wire name, not `cookie_name`: a `cookie_prefix` puts `__Host-` or
        # `__Secure-` in front, and the middleware reads only the prefixed name.
        # Seeded under the bare name the cookie was simply never found, so a test
        # asserting authenticated behaviour silently exercised the anonymous path
        # and passed anyway.
        wire_name = mw._wire_cookie_name
        sess = Session()
        existing = self._cookies.get(wire_name)
        if existing:
            try:
                decoded = mw._signer.loads(existing, max_age=max(mw.max_age, mw.permanent_lifetime))
            except BadSignature:
                decoded = None
            if isinstance(decoded, dict):
                sess = Session(decoded)

        yield sess

        self._cookies[wire_name] = mw._signer.dumps(dict(sess))

    # ── Header / cookie plumbing ──────────────────────────

    def _build_headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        return _build_request_headers(self._base_headers, self._cookies, extra)

    def _update_cookies(self, response: TestResponse) -> None:
        # Cookies the server sent persist on the client across calls.
        _apply_set_cookie_to_jar(self._cookies, response.raw_headers)

    # ── ASGI dispatch ─────────────────────────────────────

    async def _send_one_request(
        self,
        method: str,
        path: str,
        query_string: str,
        headers: dict[str, str],
        body: bytes,
        stream: Any | None = None,
    ) -> TestResponse:
        return await _send_one_request(self.app, method, path, query_string, headers, body, stream)

    def _make_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        query_string: str = "",
        follow_redirects: bool | None = None,
        stream: Any | None = None,
    ) -> TestResponse:
        if "?" in path:
            path, path_qs = path.split("?", 1)
            query_string = f"{path_qs}&{query_string}" if query_string else path_qs

        follow = self.follow_redirects if follow_redirects is None else follow_redirects
        # The redirect loop lives in the shared `_follow_redirects` driver; the
        # sync client's only specialisation is running the whole coroutine on
        # its dedicated loop (the async client awaits it instead).
        return self._loop.run_until_complete(
            _follow_redirects(
                build_headers=self._build_headers,
                send_one=self._send_one_request,
                update_cookies=self._update_cookies,
                max_redirects=self._MAX_REDIRECTS,
                base_url=self.base_url,
                follow=follow,
                client_name="TestClient",
                method=method,
                path=path,
                query_string=query_string,
                body=body,
                headers=headers,
                stream=stream,
            )
        )

    # ── Method shortcuts ──────────────────────────────────

    def get(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        """Send a GET request and return the response."""
        qs = urlencode(params) if params else ""
        return self._make_request(
            HTTP_METHOD_GET,
            path,
            headers=headers,
            query_string=qs,
            follow_redirects=follow_redirects,
        )

    def _json_or_form(
        self,
        method: str,
        path: str,
        json: Any,
        data: dict[str, str] | None,
        headers: dict[str, str] | None,
        content: bytes | None,
        files: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
        stream: Any | None = None,
    ) -> TestResponse:
        hdrs = dict(headers or {})
        body = b""
        if stream is not None:
            # A streaming body is mutually exclusive with the buffered body
            # forms; enforcing it here keeps the API misuse loud (per Veloce's
            # convention) instead of silently sending one and dropping another.
            assert json is None and data is None and content is None and files is None, (
                "stream cannot be combined with json/data/content/files"
            )
            return self._make_request(
                method, path, headers=hdrs, follow_redirects=follow_redirects, stream=stream
            )
        body, hdrs = _assemble_body(json, data, content, files, hdrs)
        return self._make_request(
            method, path, headers=hdrs, body=body, follow_redirects=follow_redirects
        )

    def post(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
        stream: Any | None = None,
    ) -> TestResponse:
        """Send a POST. `stream` feeds the body as multiple `http.request`
        chunks (a sync `Iterable` or `AsyncIterable` of `bytes`/`str`); when
        given it takes precedence over and excludes `json`/`data`/`content`/`files`.
        """
        return self._json_or_form(
            HTTP_METHOD_POST,
            path,
            json,
            data,
            headers,
            content,
            files=files,
            follow_redirects=follow_redirects,
            stream=stream,
        )

    def put(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
        stream: Any | None = None,
    ) -> TestResponse:
        """Send a PUT. See `post` for the `stream` chunked-body parameter."""
        return self._json_or_form(
            HTTP_METHOD_PUT,
            path,
            json,
            data,
            headers,
            content,
            files=files,
            follow_redirects=follow_redirects,
            stream=stream,
        )

    def patch(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
        stream: Any | None = None,
    ) -> TestResponse:
        """Send a PATCH. See `post` for the `stream` chunked-body parameter."""
        return self._json_or_form(
            HTTP_METHOD_PATCH,
            path,
            json,
            data,
            headers,
            content,
            files=files,
            follow_redirects=follow_redirects,
            stream=stream,
        )

    def delete(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        """Send a DELETE request and return the response."""
        return self._make_request(
            HTTP_METHOD_DELETE, path, headers=headers, follow_redirects=follow_redirects
        )

    def head(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        """Send a HEAD request and return the response."""
        return self._make_request(
            HTTP_METHOD_HEAD, path, headers=headers, follow_redirects=follow_redirects
        )

    def options(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        """Send an OPTIONS request and return the response."""
        return self._make_request(
            HTTP_METHOD_OPTIONS, path, headers=headers, follow_redirects=follow_redirects
        )

    def request(
        self,
        method: str,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
        follow_redirects: bool | None = None,
        stream: Any | None = None,
    ) -> TestResponse:
        """Generic request dispatcher - httpx/test-client shape.

        `client.request("PATCH", "/x", json=...)` is the verb-agnostic
        form of `client.get` / `client.post` / .... Bodies (`json` /
        `data` / `content` / `files` / `stream`) and `params` are handled
        exactly as the per-verb methods do; `stream` (see `post`) excludes
        the buffered body forms.
        """
        verb = method.upper()
        if (
            json is not None
            or data is not None
            or content is not None
            or files is not None
            or stream is not None
        ):
            return self._json_or_form(
                verb,
                path,
                json,
                data,
                headers,
                content,
                files=files,
                follow_redirects=follow_redirects,
                stream=stream,
            )
        qs = urlencode(params) if params else ""
        return self._make_request(
            verb,
            path,
            headers=headers,
            query_string=qs,
            follow_redirects=follow_redirects,
        )

    # ── WebSocket ─────────────────────────────────────────

    def websocket_connect(
        self,
        path: str,
        subprotocols: list[str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _WebSocketSession:
        """Open an in-memory WebSocket against the app - context manager.

        Drives the ASGI websocket protocol: synthesise the scope, send
        `websocket.connect`, route to the handler, then forward
        `send_text` / `receive_text` / `close` calls to the running
        handler through a pair of asyncio queues.
        """
        return _WebSocketSession(self, path, subprotocols, headers)

    # ── Lifecycle ─────────────────────────────────────────

    def close(self) -> None:
        """Run shutdown lifecycle and close the loop if we own it."""
        if self._lifespan_run and hasattr(self.app, "_run_lifecycle"):
            self._loop.run_until_complete(self.app._run_lifecycle(LIFECYCLE_SHUTDOWN))
            self._lifespan_run = False
        if self._owns_loop and not self._loop.is_closed():
            self._loop.close()
        # Hand the app back as it was found.
        if self._prior_setup_lock is not None:
            self.app._setup_lock_enabled = self._prior_setup_lock
            self._prior_setup_lock = None

    def __enter__(self) -> TestClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        if self._owns_loop and not self._loop.is_closed():
            with contextlib.suppress(Exception):
                self._loop.close()


# ── WebSocket session ─────────────────────────────────────


class _WebSocketSession:
    """Sync wrapper around an in-memory ASGI websocket session.

    The handler runs as a background task on the TestClient's loop.
    Calls to `send_text`/`receive_text`/`close` route through two
    asyncio queues that play the role the network would normally play
    - the client-side `send_text` enqueues `websocket.receive` for the
    handler, and the handler's `send_text` enqueues `websocket.send`
    back for the client to pull.
    """

    def __init__(
        self,
        client: TestClient,
        path: str,
        subprotocols: list[str] | None,
        headers: dict[str, str] | None,
    ) -> None:
        self._client = client
        self._path = path
        self._subprotocols = subprotocols or []
        self._headers = headers or {}
        self._to_handler: asyncio.Queue[dict] = asyncio.Queue()
        self._from_handler: asyncio.Queue[dict] = asyncio.Queue()
        self._handler_task: asyncio.Task | None = None
        self.accepted_subprotocol: str | None = None

    def __enter__(self) -> _WebSocketSession:
        async def _start() -> None:
            scope_headers: list[tuple[bytes, bytes]] = []
            if self._subprotocols:
                scope_headers.append(
                    (
                        b"sec-websocket-protocol",
                        ", ".join(self._subprotocols).encode("latin-1"),
                    )
                )
            seen_host = False
            for k, v in self._headers.items():
                if k.lower() == "host":
                    seen_host = True
                scope_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
            # RFC 6455 Sec. 4.1 requires the opening handshake to carry `Host`, and
            # the HTTP path already synthesises one. Without it any app running
            # host validation refuses every in-memory socket before routing.
            if not seen_host:
                scope_headers.append((b"host", b"testserver"))

            # Split any `?query` off the connect path, mirroring
            # `_make_request` for HTTP. A real ASGI server does this; the
            # in-memory client must too, or route matching sees the `?`
            # and `WebSocket.query_params` stays empty.
            ws_path, _, ws_query = self._path.partition("?")

            scope = {
                "type": ASGI_SCOPE_WEBSOCKET,
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "scheme": URL_SCHEME_WS,
                "path": unquote(ws_path),
                "raw_path": ws_path.encode("utf-8"),
                "query_string": ws_query.encode("ascii"),
                "root_path": "",
                "headers": scope_headers,
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
                "subprotocols": list(self._subprotocols),
            }

            async def receive() -> dict[str, Any]:
                return await self._to_handler.get()

            async def send(msg: dict[str, Any]) -> None:
                await self._from_handler.put(msg)

            # Kick the handshake.
            await self._to_handler.put({"type": ASGI_EVENT_WS_CONNECT})
            self._handler_task = asyncio.create_task(self._client.app(scope, receive, send))

            # Wait for the accept (or close) frame before returning.
            first = await self._from_handler.get()
            if first["type"] == ASGI_EVENT_WS_CLOSE:
                code = first.get("code", WS_1000_NORMAL_CLOSURE)
                raise RuntimeError(f"WebSocket rejected with close code {code}")
            if first["type"] != ASGI_EVENT_WS_ACCEPT:
                raise RuntimeError(
                    f"WebSocket handshake produced an unexpected ASGI message: {first['type']!r}"
                )
            self.accepted_subprotocol = first.get("subprotocol")

        self._client._loop.run_until_complete(_start())
        return self

    def __exit__(self, *exc: Any) -> None:
        async def _close() -> None:
            if self._handler_task and not self._handler_task.done():
                await self._to_handler.put(
                    {"type": ASGI_EVENT_WS_DISCONNECT, "code": WS_1000_NORMAL_CLOSURE}
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._handler_task, timeout=1.0)

        self._client._loop.run_until_complete(_close())

    def send_text(self, data: str) -> None:
        """Send a text frame to the websocket handler."""
        self._client._loop.run_until_complete(
            self._to_handler.put({"type": ASGI_EVENT_WS_RECEIVE, "text": data})
        )

    def send_bytes(self, data: bytes) -> None:
        """Send a binary frame to the websocket handler."""
        self._client._loop.run_until_complete(
            self._to_handler.put({"type": ASGI_EVENT_WS_RECEIVE, "bytes": data})
        )

    def _receive(self, key: str) -> Any:
        """Pull the next frame and return its `key` payload (`text`/`bytes`)."""

        async def _r() -> Any:
            msg = await self._from_handler.get()
            if msg.get("type") == ASGI_EVENT_WS_CLOSE:
                raise RuntimeError(f"WebSocket closed: {msg.get('code', WS_1000_NORMAL_CLOSURE)}")
            return msg[key]

        return self._client._loop.run_until_complete(_r())

    def receive_text(self) -> str:
        """Receive the next text frame sent by the handler."""
        return self._receive("text")

    def receive_bytes(self) -> bytes:
        """Receive the next binary frame sent by the handler."""
        return self._receive("bytes")

    def receive_json(self) -> Any:
        """Receive the next text frame and parse it as JSON."""
        return orjson.loads(self.receive_text())

    def send_json(self, data: Any) -> None:
        """Send `data` to the handler as a JSON-encoded text frame."""
        self.send_text(orjson.dumps(data).decode("utf-8"))


# ── Cookie jar view ───────────────────────────────────────


class _TestClientCookies:
    """Live view of a `TestClient`'s cookie jar.

    Backs onto `TestClient._cookies`. Dict-like access + iteration +
    `clear()`. Cookies added here apply to every subsequent request;
    cookies the server sends back are merged into the same backing
    dict via `TestClient._update_cookies`.
    """

    __slots__ = ("_client",)

    def __init__(self, client: TestClient | AsyncTestClient) -> None:
        self._client = client

    def __getitem__(self, key: str) -> str:
        return self._client._cookies[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._client._cookies[key] = value

    def __delitem__(self, key: str) -> None:
        del self._client._cookies[key]

    def __contains__(self, key: str) -> bool:
        return key in self._client._cookies

    def __iter__(self) -> Any:
        return iter(self._client._cookies)

    def __len__(self) -> int:
        return len(self._client._cookies)

    def get(self, key: str, default: Any = None) -> Any:
        return self._client._cookies.get(key, default)

    def clear(self) -> None:
        self._client._cookies.clear()

    def update(self, other: dict[str, str]) -> None:
        self._client._cookies.update(other)

    def items(self) -> Any:
        return self._client._cookies.items()

    def keys(self) -> Any:
        return self._client._cookies.keys()

    def values(self) -> Any:
        return self._client._cookies.values()

    def __repr__(self) -> str:
        return f"<TestClient cookies: {self._client._cookies}>"


# ── Async client ──────────────────────────────────────────


class AsyncTestClient:
    """Async in-memory test client - drives the app through its ASGI surface.

    The async counterpart of `TestClient`: used as an async context
    manager inside an async test, so each request is `await`ed on the
    test's own running event loop instead of through a private loop. The
    request methods (`get` / `post` / ...) are coroutines.

    Usage::

        async with AsyncTestClient(app) as client:
            resp = await client.get("/")

    Cookie persistence, redirect following, and the JSON / form / files
    body shapes match `TestClient` exactly. WebSocket testing stays on
    the sync `TestClient.websocket_connect`.
    """

    __test__ = False  # don't let pytest collect this as a test class

    _MAX_REDIRECTS = 10

    def __init__(
        self,
        app: Any,
        base_url: str = f"{URL_SCHEME_HTTP}://testserver",
        follow_redirects: bool = False,
    ) -> None:
        self.app = app
        self.base_url = base_url
        self.follow_redirects = follow_redirects
        self._cookies: dict[str, str] = {}
        self._base_headers: dict[str, str] = {}
        self._lifespan_run = False
        # True between `__aenter__` and `__aexit__`. Async startup work
        # cannot run in `__init__`, so requests are refused until the
        # client has been entered as a context manager.
        self._entered = False

    # ── Async context manager ─────────────────────────────

    async def __aenter__(self) -> AsyncTestClient:
        self._entered = True
        # Relax the first-request setup lock for the same reason as the sync
        # TestClient, and restore it on exit for the same reason: the app is
        # not the client's to change permanently.
        self._prior_setup_lock = None
        if hasattr(self.app, "_setup_lock_enabled"):
            self._prior_setup_lock = self.app._setup_lock_enabled
            self.app._setup_lock_enabled = False
        if hasattr(self.app, "_run_lifecycle"):
            await self.app._run_lifecycle(LIFECYCLE_STARTUP)
            self._lifespan_run = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._lifespan_run and hasattr(self.app, "_run_lifecycle"):
            await self.app._run_lifecycle(LIFECYCLE_SHUTDOWN)
            self._lifespan_run = False
        self._entered = False
        # Hand the app back as it was found.
        if getattr(self, "_prior_setup_lock", None) is not None:
            self.app._setup_lock_enabled = self._prior_setup_lock
            self._prior_setup_lock = None

    # ── Cookie management ─────────────────────────────────

    @property
    def cookies(self) -> _TestClientCookies:
        """Live view of the client's cookie jar (see `TestClient.cookies`)."""
        return _TestClientCookies(self)

    def set_cookie(self, key: str, value: str) -> None:
        """Add or update a cookie sent on every subsequent request."""
        self._cookies[key] = value

    def delete_cookie(self, key: str) -> None:
        """Remove a cookie from the jar. No-op if not present."""
        self._cookies.pop(key, None)

    # ── Header / cookie plumbing ──────────────────────────

    def _build_headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        return _build_request_headers(self._base_headers, self._cookies, extra)

    def _update_cookies(self, response: TestResponse) -> None:
        _apply_set_cookie_to_jar(self._cookies, response.raw_headers)

    # ── ASGI dispatch ─────────────────────────────────────

    async def _send_one_request(
        self,
        method: str,
        path: str,
        query_string: str,
        headers: dict[str, str],
        body: bytes,
        stream: Any | None = None,
    ) -> TestResponse:
        return await _send_one_request(self.app, method, path, query_string, headers, body, stream)

    async def _make_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        query_string: str = "",
        follow_redirects: bool | None = None,
        stream: Any | None = None,
    ) -> TestResponse:
        if not self._entered:
            raise RuntimeError(
                "AsyncTestClient must be used as an async context manager: "
                "`async with app.async_test_client() as client: ...`"
            )

        if "?" in path:
            path, path_qs = path.split("?", 1)
            query_string = f"{path_qs}&{query_string}" if query_string else path_qs

        follow = self.follow_redirects if follow_redirects is None else follow_redirects
        # Shares the `_follow_redirects` driver with the sync client - the async
        # client simply awaits the coroutine where the sync client runs it on
        # its loop. The driver keeps `current_headers` across hops, so the
        # caller's headers (Authorization, custom) reach the redirected request.
        return await _follow_redirects(
            build_headers=self._build_headers,
            send_one=self._send_one_request,
            update_cookies=self._update_cookies,
            max_redirects=self._MAX_REDIRECTS,
            base_url=self.base_url,
            follow=follow,
            client_name="AsyncTestClient",
            method=method,
            path=path,
            query_string=query_string,
            body=body,
            headers=headers,
            stream=stream,
        )

    def _assemble_body(
        self,
        json: Any,
        data: dict[str, str] | None,
        content: bytes | None,
        files: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> tuple[bytes, dict[str, str]]:
        return _assemble_body(json, data, content, files, headers)

    # ── Method shortcuts ──────────────────────────────────

    async def get(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        """Send a GET request and return the response."""
        qs = urlencode(params) if params else ""
        return await self._make_request(
            HTTP_METHOD_GET,
            path,
            headers=headers,
            query_string=qs,
            follow_redirects=follow_redirects,
        )

    async def _dispatch_body(
        self,
        method: str,
        path: str,
        json: Any,
        data: dict[str, str] | None,
        content: bytes | None,
        files: dict[str, Any] | None,
        headers: dict[str, str] | None,
        follow_redirects: bool | None,
        stream: Any | None,
    ) -> TestResponse:
        """Send a body-carrying request, routing `stream` to the chunked path."""
        if stream is not None:
            assert json is None and data is None and content is None and files is None, (
                "stream cannot be combined with json/data/content/files"
            )
            return await self._make_request(
                method,
                path,
                headers=dict(headers or {}),
                follow_redirects=follow_redirects,
                stream=stream,
            )
        body, hdrs = self._assemble_body(json, data, content, files, headers)
        return await self._make_request(
            method, path, headers=hdrs, body=body, follow_redirects=follow_redirects
        )

    async def post(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
        stream: Any | None = None,
    ) -> TestResponse:
        """Send a POST. `stream` feeds the body as multiple `http.request`
        chunks (a sync `Iterable` or `AsyncIterable` of `bytes`/`str`); when
        given it takes precedence over and excludes `json`/`data`/`content`/`files`.
        """
        return await self._dispatch_body(
            HTTP_METHOD_POST, path, json, data, content, files, headers, follow_redirects, stream
        )

    async def put(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
        stream: Any | None = None,
    ) -> TestResponse:
        """Send a PUT. See `post` for the `stream` chunked-body parameter."""
        return await self._dispatch_body(
            HTTP_METHOD_PUT, path, json, data, content, files, headers, follow_redirects, stream
        )

    async def patch(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
        stream: Any | None = None,
    ) -> TestResponse:
        """Send a PATCH. See `post` for the `stream` chunked-body parameter."""
        return await self._dispatch_body(
            HTTP_METHOD_PATCH, path, json, data, content, files, headers, follow_redirects, stream
        )

    async def delete(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        """Send a DELETE request and return the response."""
        return await self._make_request(
            HTTP_METHOD_DELETE, path, headers=headers, follow_redirects=follow_redirects
        )

    async def head(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        """Send a HEAD request and return the response."""
        return await self._make_request(
            HTTP_METHOD_HEAD, path, headers=headers, follow_redirects=follow_redirects
        )

    async def options(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        """Send an OPTIONS request and return the response."""
        return await self._make_request(
            HTTP_METHOD_OPTIONS, path, headers=headers, follow_redirects=follow_redirects
        )

    async def request(
        self,
        method: str,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
        follow_redirects: bool | None = None,
        stream: Any | None = None,
    ) -> TestResponse:
        """Generic verb-agnostic request dispatcher (see `TestClient.request`)."""
        verb = method.upper()
        if (
            json is not None
            or data is not None
            or content is not None
            or files is not None
            or stream is not None
        ):
            return await self._dispatch_body(
                verb, path, json, data, content, files, headers, follow_redirects, stream
            )
        qs = urlencode(params) if params else ""
        return await self._make_request(
            verb, path, headers=headers, query_string=qs, follow_redirects=follow_redirects
        )

    def __repr__(self) -> str:
        return f"<AsyncTestClient app={self.app!r}>"
