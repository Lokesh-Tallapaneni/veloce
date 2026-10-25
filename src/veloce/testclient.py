"""Test client — in-memory driver for the app's ASGI surface.

Constructs ASGI scopes directly and calls `app.__call__(scope, receive, send)`
on a dedicated event loop, so the radix router, dependency resolver, middleware
chain, response encoder and lifespan handshake all run as they would under a
production server. Reached through `app.test_client()` or the `TestClient`
class; the scope shape, message names and header byte-list layout follow the
ASGI specification.
"""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import secrets
import socket
import struct
import sys
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterable,
    Iterator,
    KeysView,
    Mapping,
    Sequence,
)
from typing import TYPE_CHECKING, Any
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
from veloce._internal import _quote_header_value
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
from veloce.http.cookies import iter_cookies
from veloce.status import (
    HTTP_301_MOVED_PERMANENTLY,
    HTTP_302_FOUND,
    HTTP_303_SEE_OTHER,
    HTTP_307_TEMPORARY_REDIRECT,
    HTTP_308_PERMANENT_REDIRECT,
    HTTP_500_INTERNAL_SERVER_ERROR,
    WS_1000_NORMAL_CLOSURE,
)

if TYPE_CHECKING:  # pragma: no cover
    from veloce.sessions import Session

# What a `stream=` body may be. `_aiter_body_chunks` normalises either shape
# into an async iterator of bytes, encoding `str` chunks as UTF-8.
StreamBody = Iterable[bytes | str] | AsyncIterable[bytes | str]

# ── Shared module helpers ─────────────────────────────────

# Lowercased once, so the per-header scan does not recompute it.
_SET_COOKIE_LOWER = HEADER_SET_COOKIE.lower()


def _decode_cookie_value(value: str) -> str:
    """Return what a handler is given for a jar value, undoing `dump_cookie`.

    The jar keeps the wire form so it can re-emit it verbatim; every read surface
    reports this instead, so a test asserts on what the server parses. Through
    `iter_cookies`, the one cookie reader, rather than repeating its unquoting.
    """
    for _name, decoded in iter_cookies("v=" + value):
        return decoded
    return value


def _parse_set_cookie_first_pair(value: str, *, decode: bool = True) -> tuple[str, str] | None:
    """Return the `(name, value)` of a Set-Cookie header's first segment.

    The leading `name=value` pair is the cookie itself and the rest are
    attributes; `None` when the first segment has no `=`.

    `decode=True` percent-decodes through `iter_cookies`, so a test asserts on
    what the handler is given. `decode=False` keeps the wire form the jar stores,
    which it re-emits as a browser does.
    """
    first = value.split(";", 1)[0].strip()
    if "=" not in first:
        return None
    if decode:
        for name, decoded in iter_cookies(first):
            return name, decoded
        return None
    cname, _, cval = first.partition("=")
    return cname.strip(), cval.strip()


def _resolve_redirect_location(location: str, base_url: str) -> tuple[str, str]:
    """Decompose a `Location` header into `(path, query)` for the next hop.

    With no live network an absolute URL to another host cannot be followed; a
    same-host one reduces to `path?query#fragment` and a relative one passes
    through.
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
    build_headers: Callable[[_HeadersArg], list[tuple[str, str]]],
    send_one: Callable[
        [str, str, str, list[tuple[str, str]], bytes, Any | None], Awaitable[TestResponse]
    ],
    update_cookies: Callable[[TestResponse], None],
    max_redirects: int,
    base_url: str,
    follow: bool,
    client_name: str,
    method: str,
    path: str,
    query_string: str,
    body: bytes,
    headers: _HeadersArg,
    stream: StreamBody | None,
) -> TestResponse:
    """Drive the request/redirect loop shared by both test clients.

    `send_one` is the per-hop async dispatch; how that coroutine is awaited is
    the only difference between the two clients. The status classification,
    GET-rewrite, replay guard and hop cap live here, so a redirect-semantics fix
    lands once.

    RFC 9110 Sec. 15.4: 303 always rewrites the method to GET and drops the body,
    and 301/302 follow the same browser convention here; 307/308 preserve both.
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
            # 307/308 must replay method and body, and a consumed one-shot iterator
            # cannot be re-read - so fail loudly rather than send an empty body.
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

    A sync or async iterable of `bytes` / `str`, the latter UTF-8 encoded; any
    other element type raises `TypeError`. A plain `bytes` is not a stream and is
    rejected, since raw bytes go through `content=`.
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


# Turns a read past end-of-body yields before it is told the client is gone.
# `poll_disconnected` cancels after one, so answering inside it would tell a
# streaming handler its client had gone; a real read survives and is answered.
_PEEK_TURNS = 3


class _ClientChannel:
    """The ASGI `receive` channel both test clients hand the app.

    With no `stream` a single `http.request` frame carries the whole body; with
    one, each chunk becomes a `more_body: True` frame before a terminal empty one.

    A read past end-of-body is answered `http.disconnect`, since this client has
    no socket and nothing else can arrive - but not within the turn a *peek*
    lasts. `request.is_disconnected()` cannot tell "no more input" from "the peer
    is gone", so yielding `_PEEK_TURNS` first separates the two callers by what
    they do rather than what they ask. Once the exchange is finished the answer is
    immediate, which is where a server does report the disconnect.
    """

    __slots__ = ("_body", "_chunks", "_body_sent", "_exhausted", "_finished")

    def __init__(self, body: bytes, stream: StreamBody | None) -> None:
        self._body = body
        self._chunks = None if stream is None else _aiter_body_chunks(stream)
        self._body_sent = False
        self._exhausted = False
        self._finished = asyncio.Event()

    def exchange_finished(self) -> None:
        """Record that the response is complete, so a later read answers at once."""
        self._finished.set()

    async def __call__(self) -> dict[str, Any]:
        chunks = self._chunks
        if chunks is None:
            if not self._body_sent:
                self._body_sent = True
                return {"type": ASGI_EVENT_HTTP_REQUEST, "body": self._body, "more_body": False}
        elif not self._exhausted:
            try:
                chunk = await chunks.__anext__()
            except StopAsyncIteration:
                self._exhausted = True
                return {"type": ASGI_EVENT_HTTP_REQUEST, "body": b"", "more_body": False}
            return {"type": ASGI_EVENT_HTTP_REQUEST, "body": chunk, "more_body": True}
        for _ in range(_PEEK_TURNS):
            if self._finished.is_set():
                break
            await asyncio.sleep(0)
        return {"type": ASGI_EVENT_HTTP_DISCONNECT}


#: A mapping, or ordered pairs when a header must appear more than once.
#: Private: callers pass the value, not the name of its type.
_HeadersArg = Mapping[str, str] | Sequence[tuple[str, str]] | None


def _header_pairs(headers: _HeadersArg) -> list[tuple[str, str]]:
    """Normalise a headers argument to ordered `(name, value)` pairs.

    A mapping cannot carry a header twice and HTTP allows it - two
    `Authorization` headers, several `X-Forwarded-For` hops - so pairs are
    accepted and kept in order.
    """
    if headers is None:
        return []
    if isinstance(headers, Mapping):
        return list(headers.items())
    return [(name, value) for name, value in headers]


def _has_header(pairs: list[tuple[str, str]], name: str) -> bool:
    """True when `name` is already present, whatever its case."""
    lowered = name.lower()
    return any(key.lower() == lowered for key, _ in pairs)


def _set_header(pairs: list[tuple[str, str]], name: str, value: str) -> None:
    """Replace every occurrence of `name` with one pair carrying `value`."""
    lowered = name.lower()
    pairs[:] = [(key, existing) for key, existing in pairs if key.lower() != lowered]
    pairs.append((name, value))


def _build_request_headers(
    base: dict[str, str],
    cookies: dict[str, str],
    extra: _HeadersArg,
) -> list[tuple[str, str]]:
    """Merge base headers, the cookie jar and per-call `extra` into ordered pairs.

    The single home for the merge order, so the sync and async test clients
    stay in lockstep.
    """
    extra_pairs = _header_pairs(extra)
    # A per-call header replaces a client-level one of the same name, but repeats
    # within the call are kept - which is why overridden names are dropped first.
    overridden = {name.lower() for name, _ in extra_pairs}
    merged = [(name, value) for name, value in base.items() if name.lower() not in overridden]
    if cookies and "cookie" not in overridden:
        merged.append(("cookie", "; ".join(f"{k}={v}" for k, v in cookies.items())))
    merged.extend(extra_pairs)
    return merged


def _apply_set_cookie_to_jar(jar: dict[str, str], raw_headers: list[tuple[bytes, bytes]]) -> None:
    """Update `jar` from the response's `Set-Cookie` headers.

    Honours `Max-Age=0` as a deletion signal (RFC 6265 Sec. 5.2.2). Both test
    clients share this, so a fix to the cookie semantics applies to sync and
    async at once.
    """
    for name_bytes, value_bytes in raw_headers:
        name = name_bytes.decode("latin-1")
        if name.lower() != _SET_COOKIE_LOWER:
            continue
        value = value_bytes.decode("latin-1")
        # `decode=False`: the jar re-emits the bytes verbatim, as a browser does.
        pair = _parse_set_cookie_first_pair(value, decode=False)
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

    The file-like's `.type` when present, else `mimetypes.guess_type`, else the
    `application/octet-stream` the multipart spec recommends for an unknown one.
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

    `files` shape per RFC 7578 Sec. 4, mirroring `requests` / `httpx` - values
    can be:
    - `bytes` / `str`: raw file content, filename inferred from key.
    - file-like (anything with `.read()`): filename from `.name`, else the key.
    - `(filename, content_or_filelike)`: 2-tuple.
    - `(filename, content_or_filelike, content_type)`: 3-tuple.

    A shape with no explicit content-type has one guessed from the filename.
    `fields` are non-file parts; both appear in registration order. Returns
    `(body, content_type)`, where the content type carries the random boundary.
    """

    def _q(value: str) -> str:
        # Per RFC 7578 Sec. 4.2 / RFC 2616 Sec. 2.2 quoted-string: escape `"` and
        # `\`, and reject CR / LF, which would inject header fields into the preamble.
        if "\r" in value or "\n" in value:
            raise ValueError("multipart name / filename must not contain CR or LF")
        return _quote_header_value(value)

    boundary = "----veloce-" + secrets.token_hex(16)
    parts: list[bytes] = []
    b = boundary.encode("ascii")

    for name, value in fields.items():
        parts.append(b"--" + b + b"\r\n")
        parts.append(f'Content-Disposition: form-data; name="{_q(name)}"\r\n\r\n'.encode())
        parts.append(value.encode("utf-8") if isinstance(value, str) else value)
        parts.append(b"\r\n")

    for name, spec in files.items():
        # Match `requests` / `httpx`: bytes / str, a file-like (anything with
        # `.read()`), or a 2- or 3-tuple adding the filename and content type.
        if isinstance(spec, (bytes, str)):
            filename, content, ct = name, spec, MIME_OCTET_STREAM
        elif isinstance(spec, tuple) and len(spec) == 2:
            filename, content = spec
            ct = _guess_content_type(filename, content)
        elif isinstance(spec, tuple) and len(spec) == 3:
            filename, content, ct = spec
        elif hasattr(spec, "read"):
            # `.name` when present (an open file sets it, BytesIO does not).
            filename = getattr(spec, "name", None) or name
            content = spec
            ct = _guess_content_type(filename, content)
        else:
            raise TypeError(f"files[{name!r}] must be bytes, str, file-like, or 2/3-tuple")
        # Resolve the body bytes for any file-like content.
        read = getattr(content, "read", None)
        if read is not None:
            content = read()
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not isinstance(content, bytes):
            raise TypeError(f"files[{name!r}] content must be bytes, str, or file-like")
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
    headers: list[tuple[str, str]],
    body: bytes,
    stream: StreamBody | None = None,
) -> TestResponse:
    """Drive one ASGI request through `app` and collect its response.

    Shared by both clients so a change to the scope or receive channel cannot
    reach one and miss the other.
    """
    scope = _build_scope(method, path, query_string, headers)
    return await _collect_response(app, scope, _ClientChannel(body, stream))


def _prepare_body_request(
    json: Any,
    data: dict[str, str] | None,
    content: bytes | None,
    files: dict[str, Any] | None,
    headers: _HeadersArg,
    stream: StreamBody | None,
) -> tuple[list[tuple[str, str]], bytes | None]:
    """Return `(headers, body)` for a body-carrying request.

    `body` is `None` when `stream` was given, which excludes every buffered form.
    Enforced here, so the misuse is loud rather than one argument silently
    winning, and so both clients answer the same way.
    """
    if stream is not None:
        assert json is None and data is None and content is None and files is None, (
            "stream cannot be combined with json/data/content/files"
        )
        return _header_pairs(headers), None
    body, hdrs = _assemble_body(json, data, content, files, headers)
    return hdrs, body


def _assemble_body(
    json: Any,
    data: dict[str, str] | None,
    content: bytes | None,
    files: dict[str, Any] | None,
    headers: _HeadersArg,
) -> tuple[bytes, list[tuple[str, str]]]:
    """Encode one buffered request body and the content type it implies.

    Shared by both clients so the sync and async paths cannot drift on which
    argument wins or on the media type each form sets.
    """
    hdrs = _header_pairs(headers)
    body = b""
    if files is not None:
        body, ct = _encode_multipart(files, data or {})
        _set_header(hdrs, HEADER_CONTENT_TYPE, ct)
    elif json is not None:
        body = orjson.dumps(json)
        if not _has_header(hdrs, HEADER_CONTENT_TYPE):
            hdrs.append((HEADER_CONTENT_TYPE, MIME_JSON))
    elif data is not None:
        body = urlencode(data).encode()
        if not _has_header(hdrs, HEADER_CONTENT_TYPE):
            hdrs.append((HEADER_CONTENT_TYPE, MIME_FORM_URLENCODED))
    elif content is not None:
        body = content
    return body, hdrs


def _build_scope(
    method: str,
    path: str,
    query_string: str,
    headers: list[tuple[str, str]],
    client: tuple[str, int] = ("testclient", 50000),
    server: tuple[str, int] = ("testserver", 80),
    scheme: str = URL_SCHEME_HTTP,
    root_path: str = "",
) -> dict[str, Any]:
    """Build an ASGI 3.0 HTTP scope. Header values are encoded latin-1 per spec."""
    raw_headers: list[tuple[bytes, bytes]] = []
    seen_host = False
    for k, v in headers:
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
        # ASGI defines `path` percent-decoded, as a real server hands it to the app,
        # or a decoding-dependent bug is invisible to the in-process suite.
        "path": unquote(path),
        # ASGI permits non-ASCII bytes in `raw_path`, so `encode('ascii')` would
        # crash on a non-ASCII URL. This stays undecoded - it is the raw target.
        "raw_path": path.encode("utf-8"),
        "query_string": query_string.encode("ascii"),
        "root_path": root_path,
        "headers": raw_headers,
        "client": client,
        "server": server,
    }


async def _collect_response(
    app: Any, scope: dict[str, Any], receive: _ClientChannel
) -> TestResponse:
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
            if not message.get("more_body", False):
                # End of response: where a server starts reporting `http.disconnect`, so
                # from here a read past end-of-body is answered without yielding.
                receive.exchange_finished()

    try:
        await app(scope, receive, send)
    finally:
        # An app that raised, or ended without a terminal body message, never
        # reached the branch above; the exchange is over either way.
        receive.exchange_finished()
    return TestResponse(status_code, b"".join(body_chunks), raw_headers)


#: `SO_LINGER` with `onoff=1, linger=0` - close sends RST, so the socket skips
#: `TIME_WAIT`. Windows lays `linger` out as two `u_short`, POSIX as two `int`.
_ABORTIVE_CLOSE = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)


def _abort_self_pipe_on_close(loop: asyncio.AbstractEventLoop) -> None:
    """Make the selector loop's wakeup pipe release its port on close.

    The pipe is a loopback TCP socketpair, and a normal close leaves one end in
    `TIME_WAIT` for minutes - one ephemeral port per client, against a dynamic
    range only 16384 wide. It carries wakeup bytes nothing reads at shutdown, so
    an abortive close loses nothing.

    Only the loops `_new_loop` builds, so this slows the rate at which the range
    is consumed rather than removing the failure. `_ssock` / `_csock` are CPython
    internals, so a rename gets today's behaviour back rather than an exception.
    """
    for name in ("_ssock", "_csock"):
        sock = getattr(loop, name, None)
        if sock is not None:
            # Best effort: a family refusing `SO_LINGER` must not fail every test.
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _ABORTIVE_CLOSE)


def _new_loop() -> asyncio.AbstractEventLoop:
    """Build the event loop a test client drives its app on.

    On Windows the default proactor loop runs an I/O completion port per
    iteration, and the in-memory client opens no socket for it to find: the
    selector loop measured 15.5% more requests per second here. It costs a
    socketpair, whose port `_abort_self_pipe_on_close` hands straight back.

    It cannot run `asyncio.create_subprocess_*`, so a handler that spawns one
    takes `TestClient(app, loop=asyncio.ProactorEventLoop())` instead.
    """
    if sys.platform == "win32":
        loop = asyncio.SelectorEventLoop()
        _abort_self_pipe_on_close(loop)
        return loop
    return asyncio.new_event_loop()


class TestResponse:
    """What every `TestClient` / `AsyncTestClient` call returns.

    A read-only view of one ASGI response cycle: `status_code`, the decoded
    `text` and parsed `json()`, a case-insensitive `headers` mapping, the parsed
    `cookies`, and `raw_headers` for what the mapping flattens (several
    `Set-Cookie` lines join into one `headers` value). The clients build it.

    Usage::

        with TestClient(app) as client:
            response = client.get("/items/1")

        assert response.status_code == 200
        assert response.json() == {"id": 1}
        assert response.headers["content-type"].startswith("application/json")
    """

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
        # Case-insensitive, so `headers["Content-Type"]` works whether or not the app
        # lower-cased its keys. Set-Cookie joins here; `raw_headers` keeps the list.
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

        # Relaxed so a test can register routes between calls, and restored on
        # `close()`, or the app would never freeze its route table when served.
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

        Dict-like access, assignment, deletion, iteration and `clear()`. Cookies
        the server sends are merged in, and the state persists across calls until
        the client is closed.
        """
        return _TestClientCookies(self)

    def set_cookie(self, key: str, value: str) -> None:
        """Add or update a cookie sent on every subsequent request."""
        self._cookies[key] = value

    def delete_cookie(self, key: str) -> None:
        """Remove a cookie from the jar. No-op if not present."""
        self._cookies.pop(key, None)

    def wait_for_background_tasks(self, timeout: float | None = 5.0) -> bool:
        """Wait for the app's background tasks to finish, on this client's loop.

        A background task runs after the response is returned, so a test
        asserting its effect would otherwise drive `client._loop` by hand.
        Returns `True` when everything finished, `False` on timeout.

        Usage::

            client.post("/subscribe")
            client.wait_for_background_tasks()
            assert sent == ["welcome@example.com"]
        """
        return self._loop.run_until_complete(self.app.wait_for_background_tasks(timeout))

    @contextlib.contextmanager
    def session_transaction(self) -> Generator[Session, None, None]:
        """Mutate the session outside a request.

        Yields a `Session` dict pre-loaded from the current session cookie. On
        exit it is re-signed with the app's `SessionMiddleware` secret and stored
        in the cookie jar, so the next request carries it::

            with client.session_transaction() as sess:
                sess["user_id"] = 7

        Raises `RuntimeError` if the app has no `SessionMiddleware`.
        """
        from veloce.middleware.sessions import SessionMiddleware, SessionMiddlewareBase
        from veloce.sessions import Session

        registered = self.app.middlewares
        mw = next((m for m in registered if isinstance(m, SessionMiddleware)), None)
        if mw is None:
            # Distinguished from "no session at all", which is a different mistake.
            other = next((m for m in registered if isinstance(m, SessionMiddlewareBase)), None)
            if other is not None:
                raise RuntimeError(
                    f"session_transaction() seeds a signed cookie, which "
                    f"{type(other).__name__} does not use - write to its store directly instead"
                )
            raise RuntimeError("session_transaction() requires SessionMiddleware on the app")

        # The signing key may still be waiting on `SECRET_KEY`, which is settled on
        # the first request - so ask the middleware rather than resolve it here.
        mw.bind_secret_key(self.app.config, hint="before session_transaction()")

        # The wire name, not `cookie_name`: a `cookie_prefix` prepends `__Host-` or
        # `__Secure-`, and the middleware reads only the prefixed name.
        wire_name = mw.wire_cookie_name
        sess = Session()
        existing = self._cookies.get(wire_name)
        if existing:
            # The middleware's own decode, so the age ceiling applies here too.
            decoded = mw.decode_cookie(existing)
            if decoded is not None:
                sess = Session(decoded)

        yield sess

        self._cookies[wire_name] = mw.encode_cookie(sess)

    # ── Header / cookie plumbing ──────────────────────────

    def _build_headers(self, extra: _HeadersArg) -> list[tuple[str, str]]:
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
        headers: list[tuple[str, str]],
        body: bytes,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        return await _send_one_request(self.app, method, path, query_string, headers, body, stream)

    def _make_request(
        self,
        method: str,
        path: str,
        headers: _HeadersArg = None,
        body: bytes = b"",
        query_string: str = "",
        follow_redirects: bool | None = None,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        if "?" in path:
            path, path_qs = path.split("?", 1)
            query_string = f"{path_qs}&{query_string}" if query_string else path_qs

        follow = self.follow_redirects if follow_redirects is None else follow_redirects
        # The shared `_follow_redirects` driver; the sync client's only
        # specialisation is running the coroutine on its dedicated loop.
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
        headers: _HeadersArg = None,
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

    def _dispatch_body(
        self,
        method: str,
        path: str,
        json: Any,
        data: dict[str, str] | None,
        content: bytes | None,
        files: dict[str, Any] | None,
        headers: _HeadersArg,
        follow_redirects: bool | None,
        stream: StreamBody | None,
    ) -> TestResponse:
        """Send a body-carrying request, routing `stream` to the chunked path."""
        hdrs, body = _prepare_body_request(json, data, content, files, headers, stream)
        if body is None:
            return self._make_request(
                method, path, headers=hdrs, follow_redirects=follow_redirects, stream=stream
            )
        return self._make_request(
            method, path, headers=hdrs, body=body, follow_redirects=follow_redirects
        )

    def post(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: _HeadersArg = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        """Send a POST.

        `stream` feeds the body as multiple `http.request` chunks (a sync
        `Iterable` or `AsyncIterable` of `bytes`/`str`); when given it takes
        precedence over and excludes `json`/`data`/`content`/`files`.
        """
        return self._dispatch_body(
            HTTP_METHOD_POST,
            path,
            json=json,
            data=data,
            headers=headers,
            content=content,
            files=files,
            follow_redirects=follow_redirects,
            stream=stream,
        )

    def put(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: _HeadersArg = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        """Send a PUT. See `post` for the `stream` chunked-body parameter."""
        return self._dispatch_body(
            HTTP_METHOD_PUT,
            path,
            json=json,
            data=data,
            headers=headers,
            content=content,
            files=files,
            follow_redirects=follow_redirects,
            stream=stream,
        )

    def patch(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: _HeadersArg = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        """Send a PATCH. See `post` for the `stream` chunked-body parameter."""
        return self._dispatch_body(
            HTTP_METHOD_PATCH,
            path,
            json=json,
            data=data,
            headers=headers,
            content=content,
            files=files,
            follow_redirects=follow_redirects,
            stream=stream,
        )

    def delete(
        self,
        path: str,
        headers: _HeadersArg = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        """Send a DELETE request and return the response."""
        return self._make_request(
            HTTP_METHOD_DELETE, path, headers=headers, follow_redirects=follow_redirects
        )

    def head(
        self,
        path: str,
        headers: _HeadersArg = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        """Send a HEAD request and return the response."""
        return self._make_request(
            HTTP_METHOD_HEAD, path, headers=headers, follow_redirects=follow_redirects
        )

    def options(
        self,
        path: str,
        headers: _HeadersArg = None,
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
        headers: _HeadersArg = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
        follow_redirects: bool | None = None,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        """Dispatch a request of any verb - httpx/test-client shape.

        The verb-agnostic form of `client.get` / `client.post` / .... Bodies and
        `params` are handled exactly as the per-verb methods do, and `stream`
        excludes the buffered body forms.
        """
        verb = method.upper()
        if (
            json is not None
            or data is not None
            or content is not None
            or files is not None
            or stream is not None
        ):
            return self._dispatch_body(
                verb,
                path,
                json=json,
                data=data,
                headers=headers,
                content=content,
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
        headers: _HeadersArg = None,
    ) -> _WebSocketSession:
        """Open an in-memory WebSocket against the app - context manager.

        Synthesises the scope, sends `websocket.connect`, routes to the handler,
        then forwards calls to it through a pair of asyncio queues.
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
        # Read through `getattr`, or an `AttributeError` here would surface above a
        # constructor error. The lock is restored here as well as in `close()`, since
        # a client used without its context manager would leave it off for good.
        prior = getattr(self, "_prior_setup_lock", None)
        if prior is not None:
            with contextlib.suppress(Exception):
                self.app._setup_lock_enabled = prior
                self._prior_setup_lock = None
        loop = getattr(self, "_loop", None)
        if getattr(self, "_owns_loop", False) and loop is not None and not loop.is_closed():
            with contextlib.suppress(Exception):
                loop.close()


# ── WebSocket session ─────────────────────────────────────


class _WebSocketSession:
    """Sync wrapper around an in-memory ASGI websocket session.

    The handler runs as a background task on the TestClient's loop, and
    `send_text` / `receive_text` / `close` route through two asyncio queues that
    play the part the network would.
    """

    def __init__(
        self,
        client: TestClient,
        path: str,
        subprotocols: list[str] | None,
        headers: _HeadersArg,
    ) -> None:
        self._client = client
        self._path = path
        self._subprotocols = subprotocols or []
        self._headers = headers or {}
        self._to_handler: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_handler: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._handler_task: asyncio.Task[None] | None = None
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
            for k, v in _header_pairs(self._headers):
                if k.lower() == "host":
                    seen_host = True
                scope_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
            # RFC 6455 Sec. 4.1 requires `Host` on the opening handshake; without it an
            # app running host validation refuses every in-memory socket.
            if not seen_host:
                scope_headers.append((b"host", b"testserver"))

            # Split as `_make_request` does, or route matching sees the `?` and
            # `WebSocket.query_params` stays empty.
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

    Backs onto `TestClient._cookies`, so a cookie added here applies to every
    later request and one the server sends merges into the same dict.
    """

    __slots__ = ("_client",)

    def __init__(self, client: TestClient | AsyncTestClient) -> None:
        self._client = client

    def __getitem__(self, key: str) -> str:
        return _decode_cookie_value(self._client._cookies[key])

    def __setitem__(self, key: str, value: str) -> None:
        self._client._cookies[key] = value

    def __delitem__(self, key: str) -> None:
        del self._client._cookies[key]

    def __contains__(self, key: str) -> bool:
        return key in self._client._cookies

    def __iter__(self) -> Iterator[str]:
        return iter(self._client._cookies)

    def __len__(self) -> int:
        return len(self._client._cookies)

    def get(self, key: str, default: str | None = None) -> str | None:
        raw = self._client._cookies.get(key)
        return default if raw is None else _decode_cookie_value(raw)

    def clear(self) -> None:
        self._client._cookies.clear()

    def update(self, other: dict[str, str]) -> None:
        self._client._cookies.update(other)

    def items(self) -> list[tuple[str, str]]:
        return [(k, _decode_cookie_value(v)) for k, v in self._client._cookies.items()]

    def keys(self) -> KeysView[str]:
        return self._client._cookies.keys()

    def values(self) -> list[str]:
        return [_decode_cookie_value(v) for v in self._client._cookies.values()]

    def __repr__(self) -> str:
        return f"<TestClient cookies: {dict(self.items())}>"


# ── Async client ──────────────────────────────────────────


class AsyncTestClient:
    """Async in-memory test client - drives the app through its ASGI surface.

    Used as an async context manager inside an async test, so each request is
    awaited on the test's own running loop rather than a private one.

    Usage::

        async with AsyncTestClient(app) as client:
            resp = await client.get("/")

    Cookie persistence, redirect following and the body shapes match `TestClient`
    exactly. WebSocket testing and `session_transaction` stay on `TestClient`.
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
        # Set in `__aenter__`, restored in `__aexit__`. No `__del__` counterpart to
        # the sync client's: a finaliser cannot await the shutdown lifecycle.
        self._prior_setup_lock: bool | None = None
        # Async startup cannot run in `__init__`, so requests are refused until entry.
        self._entered = False

    # ── Async context manager ─────────────────────────────

    async def wait_for_background_tasks(self, timeout: float | None = 5.0) -> bool:
        """Wait for the app's background tasks to finish. See `TestClient`."""
        return await self.app.wait_for_background_tasks(timeout)

    async def __aenter__(self) -> AsyncTestClient:
        self._entered = True
        # Relaxed and restored for the same reasons as on the sync TestClient.
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
        if self._prior_setup_lock is not None:
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

    def _build_headers(self, extra: _HeadersArg) -> list[tuple[str, str]]:
        return _build_request_headers(self._base_headers, self._cookies, extra)

    def _update_cookies(self, response: TestResponse) -> None:
        _apply_set_cookie_to_jar(self._cookies, response.raw_headers)

    # ── ASGI dispatch ─────────────────────────────────────

    async def _send_one_request(
        self,
        method: str,
        path: str,
        query_string: str,
        headers: list[tuple[str, str]],
        body: bytes,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        return await _send_one_request(self.app, method, path, query_string, headers, body, stream)

    async def _make_request(
        self,
        method: str,
        path: str,
        headers: _HeadersArg = None,
        body: bytes = b"",
        query_string: str = "",
        follow_redirects: bool | None = None,
        stream: StreamBody | None = None,
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
        # The shared `_follow_redirects` driver, awaited rather than run on a loop.
        # It keeps `current_headers` across hops, so the caller's reach the redirect.
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

    # ── Method shortcuts ──────────────────────────────────

    async def get(
        self,
        path: str,
        headers: _HeadersArg = None,
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
        headers: _HeadersArg,
        follow_redirects: bool | None,
        stream: StreamBody | None,
    ) -> TestResponse:
        """Send a body-carrying request, routing `stream` to the chunked path."""
        hdrs, body = _prepare_body_request(json, data, content, files, headers, stream)
        if body is None:
            return await self._make_request(
                method, path, headers=hdrs, follow_redirects=follow_redirects, stream=stream
            )
        return await self._make_request(
            method, path, headers=hdrs, body=body, follow_redirects=follow_redirects
        )

    async def post(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: _HeadersArg = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        """Send a POST.

        `stream` feeds the body as multiple `http.request` chunks (a sync
        `Iterable` or `AsyncIterable` of `bytes`/`str`); when given it takes
        precedence over and excludes `json`/`data`/`content`/`files`.
        """
        return await self._dispatch_body(
            HTTP_METHOD_POST,
            path,
            json=json,
            data=data,
            content=content,
            files=files,
            headers=headers,
            follow_redirects=follow_redirects,
            stream=stream,
        )

    async def put(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: _HeadersArg = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        """Send a PUT. See `post` for the `stream` chunked-body parameter."""
        return await self._dispatch_body(
            HTTP_METHOD_PUT,
            path,
            json=json,
            data=data,
            content=content,
            files=files,
            headers=headers,
            follow_redirects=follow_redirects,
            stream=stream,
        )

    async def patch(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: _HeadersArg = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        """Send a PATCH. See `post` for the `stream` chunked-body parameter."""
        return await self._dispatch_body(
            HTTP_METHOD_PATCH,
            path,
            json=json,
            data=data,
            content=content,
            files=files,
            headers=headers,
            follow_redirects=follow_redirects,
            stream=stream,
        )

    async def delete(
        self,
        path: str,
        headers: _HeadersArg = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        """Send a DELETE request and return the response."""
        return await self._make_request(
            HTTP_METHOD_DELETE, path, headers=headers, follow_redirects=follow_redirects
        )

    async def head(
        self,
        path: str,
        headers: _HeadersArg = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        """Send a HEAD request and return the response."""
        return await self._make_request(
            HTTP_METHOD_HEAD, path, headers=headers, follow_redirects=follow_redirects
        )

    async def options(
        self,
        path: str,
        headers: _HeadersArg = None,
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
        headers: _HeadersArg = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
        follow_redirects: bool | None = None,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        """Dispatch a request of any verb (see `TestClient.request`)."""
        verb = method.upper()
        if (
            json is not None
            or data is not None
            or content is not None
            or files is not None
            or stream is not None
        ):
            return await self._dispatch_body(
                verb,
                path,
                json=json,
                data=data,
                content=content,
                files=files,
                headers=headers,
                follow_redirects=follow_redirects,
                stream=stream,
            )
        qs = urlencode(params) if params else ""
        return await self._make_request(
            verb, path, headers=headers, query_string=qs, follow_redirects=follow_redirects
        )

    def __repr__(self) -> str:
        return f"<AsyncTestClient app={self.app!r}>"
