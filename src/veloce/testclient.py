"""Test client — in-memory driver for the app's ASGI surface.

Constructs ASGI scopes directly and calls `app.__call__(scope, receive, send)`
on a dedicated event loop. This exercises the same path a production ASGI
server would take — the radix router, dependency resolver, middleware chain,
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
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlencode, urlparse

import orjson
from multidict import CIMultiDict


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
        # Fragments are stripped by browsers on redirect — drop them.
        return parsed.path or "/", parsed.query
    # Relative location (or odd scheme-only) — use verbatim, splitting any qs.
    path, _, query = location.partition("?")
    return path, query


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
            if name.lower() == "set-cookie":
                set_cookies.append(value)
            else:
                flat[name] = value
        if set_cookies:
            flat["Set-Cookie"] = "\r\nSet-Cookie: ".join(set_cookies)
        self.headers = flat
        self.content_type = flat.get("content-type") or ""
        # Parse cookies from all Set-Cookie headers; each cookie's first
        # `name=value` segment wins.
        self.cookies: dict[str, str] = {}
        for line in set_cookies:
            first = line.split(";", 1)[0].strip()
            if "=" in first:
                cname, _, cval = first.partition("=")
                self.cookies[cname.strip()] = cval.strip()

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
    as a deletion signal (RFC 6265 §5.2.2). Both test clients share this
    so a fix to the cookie semantics applies to sync + async at once.
    """
    for name_bytes, value_bytes in raw_headers:
        name = name_bytes.decode("latin-1")
        if name.lower() != "set-cookie":
            continue
        value = value_bytes.decode("latin-1")
        first = value.split(";", 1)[0].strip()
        if "=" not in first:
            continue
        cname, _, cval = first.partition("=")
        cname = cname.strip()
        rest = value.split(";")[1:]
        is_deletion = any("max-age=0" in seg.strip().lower() for seg in rest)
        if is_deletion:
            jar.pop(cname, None)
        else:
            jar[cname] = cval.strip()


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
    return "application/octet-stream"


def _encode_multipart(
    files: dict[str, Any],
    fields: dict[str, str],
) -> tuple[bytes, str]:
    """Build a `multipart/form-data` body from files + extra form fields.

    `files` shape per RFC 7578 §4 — values can be:
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
        # Per RFC 7578 §4.2 / RFC 2616 §2.2 quoted-string: escape `"` and
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
        # object with `.read()` — BytesIO, open file handle, IO[bytes]),
        # 2-tuple `(filename, content_or_filelike)`, or 3-tuple
        # `(filename, content_or_filelike, content_type)`. Tests
        # migrating from `requests.post(files={"f": BytesIO(...)})`
        # used to crash with a TypeError here.
        if isinstance(spec, (bytes, str)):
            filename, content, ct = name, spec, "application/octet-stream"
        elif isinstance(spec, tuple) and len(spec) == 2:
            filename, content = spec
            ct = _guess_content_type(filename, content)
        elif isinstance(spec, tuple) and len(spec) == 3:
            filename, content, ct = spec
        elif hasattr(spec, "read"):
            # Bare file-like — pull filename from `.name` when present
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
    return body, f"multipart/form-data; boundary={boundary}"


def _build_scope(
    method: str,
    path: str,
    query_string: str,
    headers: dict[str, str],
    client: tuple[str, int] = ("testclient", 50000),
    server: tuple[str, int] = ("testserver", 80),
    scheme: str = "http",
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
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": scheme,
        "path": path,
        # ASGI permits non-ASCII bytes in `raw_path` (UTF-8 is the only
        # universally-decodable encoding for percent-decoded paths). Plain
        # `path.encode("ascii")` would crash the moment a test reached a
        # non-ASCII URL.
        "raw_path": path.encode("utf-8"),
        "query_string": query_string.encode("ascii"),
        "root_path": root_path,
        "headers": raw_headers,
        "client": client,
        "server": server,
    }


class TestClient:
    """Sync test client — drives the app through its ASGI surface."""

    __test__ = False  # don't let pytest collect this as a test class

    # Hop cap for redirect chasing. Matches httpx's default. Loops or
    # ping-pong redirects raise rather than silently spin.
    _MAX_REDIRECTS = 10

    def __init__(
        self,
        app: Any,
        base_url: str = "http://testserver",
        follow_redirects: bool = False,
    ) -> None:
        self.app = app
        self.base_url = base_url
        self.follow_redirects = follow_redirects
        self._cookies: dict[str, str] = {}
        self._base_headers: dict[str, str] = {}
        self._owns_loop = False
        self._lifespan_run = False

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
            self._owns_loop = True

        # Run startup lifecycle once at construction so users can mutate
        # app.state etc. in startup hooks before the first call.
        if hasattr(app, "_run_lifecycle"):
            self._loop.run_until_complete(app._run_lifecycle("startup"))
            self._lifespan_run = True

    # ── Cookie management (conventional shape) ───────────────────────

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
        from veloce.middleware.sessions import SessionMiddleware
        from veloce.sessions import Session
        from veloce.signing import BadSignature

        mw = next(
            (m for m in getattr(self.app, "_middlewares", []) if isinstance(m, SessionMiddleware)),
            None,
        )
        if mw is None:
            raise RuntimeError("session_transaction() requires SessionMiddleware on the app")

        sess = Session()
        existing = self._cookies.get(mw.cookie_name)
        if existing:
            try:
                decoded = mw._signer.loads(existing, max_age=max(mw.max_age, mw.permanent_lifetime))
            except BadSignature:
                decoded = None
            if isinstance(decoded, dict):
                sess = Session(decoded)

        yield sess

        self._cookies[mw.cookie_name] = mw._signer.dumps(dict(sess))

    # ── Header / cookie plumbing ─────────────────────────────────────

    def _build_headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        return _build_request_headers(self._base_headers, self._cookies, extra)

    def _update_cookies(self, response: TestResponse) -> None:
        # Cookies the server sent persist on the client across calls.
        _apply_set_cookie_to_jar(self._cookies, response.raw_headers)

    # ── ASGI dispatch ────────────────────────────────────────────────

    async def _send_one_request(
        self,
        method: str,
        path: str,
        query_string: str,
        headers: dict[str, str],
        body: bytes,
    ) -> TestResponse:
        scope = _build_scope(method, path, query_string, headers)

        body_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if body_sent:
                # ASGI middleware that legitimately reads past end-of-body
                # (introspection, fan-out, replay) should see a clean
                # `http.disconnect` rather than hang forever on a
                # never-set Event. The old behaviour leaked the coroutine
                # and froze the test on the second receive.
                return {"type": "http.disconnect"}
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        status_code = 500
        raw_headers: list[tuple[bytes, bytes]] = []
        body_chunks: list[bytes] = []

        async def send(message: dict[str, Any]) -> None:
            nonlocal status_code, raw_headers
            mtype = message["type"]
            if mtype == "http.response.start":
                status_code = message["status"]
                raw_headers = list(message.get("headers") or [])
            elif mtype == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    body_chunks.append(chunk)
                # `more_body` False signals end of response.

        await self.app(scope, receive, send)

        return TestResponse(status_code, b"".join(body_chunks), raw_headers)

    def _make_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        query_string: str = "",
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        if "?" in path:
            path, path_qs = path.split("?", 1)
            query_string = f"{path_qs}&{query_string}" if query_string else path_qs

        follow = self.follow_redirects if follow_redirects is None else follow_redirects
        current_method = method
        current_path = path
        current_query = query_string
        current_body = body
        current_headers = headers

        for _ in range(self._MAX_REDIRECTS + 1):
            all_headers = self._build_headers(current_headers)
            resp = self._loop.run_until_complete(
                self._send_one_request(
                    current_method, current_path, current_query, all_headers, current_body
                )
            )
            self._update_cookies(resp)
            if not follow or resp.status_code not in (301, 302, 303, 307, 308):
                return resp
            location = resp.headers.get("location") or resp.headers.get("Location")
            if not location:
                return resp
            # RFC 9110 §15.4: 303 always changes the method to GET and
            # drops the body. 301/302 historically did the same (browsers
            # all do); modern recommendation is to preserve, but for
            # test-client predictability we follow the browser convention.
            # 307/308 strictly preserve method+body.
            new_method = current_method
            new_body = current_body
            if resp.status_code in (301, 302, 303):
                new_method = "GET"
                new_body = b""
            new_path, new_query = _resolve_redirect_location(location, self.base_url)
            current_method = new_method
            current_path = new_path
            current_query = new_query
            current_body = new_body
        raise RuntimeError(
            f"TestClient exceeded {self._MAX_REDIRECTS} redirects following {method} {path}"
        )

    # ── Method shortcuts ─────────────────────────────────────────────

    def get(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        qs = urlencode(params) if params else ""
        return self._make_request(
            "GET", path, headers=headers, query_string=qs, follow_redirects=follow_redirects
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
    ) -> TestResponse:
        hdrs = dict(headers or {})
        body = b""
        if files is not None:
            body, ct = _encode_multipart(files, data or {})
            hdrs["content-type"] = ct
        elif json is not None:
            body = orjson.dumps(json)
            hdrs.setdefault("content-type", "application/json")
        elif data is not None:
            body = urlencode(data).encode()
            hdrs.setdefault("content-type", "application/x-www-form-urlencoded")
        elif content is not None:
            body = content
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
    ) -> TestResponse:
        return self._json_or_form(
            "POST",
            path,
            json,
            data,
            headers,
            content,
            files=files,
            follow_redirects=follow_redirects,
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
    ) -> TestResponse:
        return self._json_or_form(
            "PUT",
            path,
            json,
            data,
            headers,
            content,
            files=files,
            follow_redirects=follow_redirects,
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
    ) -> TestResponse:
        return self._json_or_form(
            "PATCH",
            path,
            json,
            data,
            headers,
            content,
            files=files,
            follow_redirects=follow_redirects,
        )

    def delete(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        return self._make_request(
            "DELETE", path, headers=headers, follow_redirects=follow_redirects
        )

    def head(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        return self._make_request("HEAD", path, headers=headers, follow_redirects=follow_redirects)

    def options(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        return self._make_request(
            "OPTIONS", path, headers=headers, follow_redirects=follow_redirects
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
    ) -> TestResponse:
        """Generic request dispatcher — httpx/test-client shape.

        `client.request("PATCH", "/x", json=...)` is the verb-agnostic
        form of `client.get` / `client.post` / …. Bodies (`json` /
        `data` / `content` / `files`) and `params` are handled exactly
        as the per-verb methods do.
        """
        verb = method.upper()
        if json is not None or data is not None or content is not None or files is not None:
            return self._json_or_form(
                verb,
                path,
                json,
                data,
                headers,
                content,
                files=files,
                follow_redirects=follow_redirects,
            )
        qs = urlencode(params) if params else ""
        return self._make_request(
            verb,
            path,
            headers=headers,
            query_string=qs,
            follow_redirects=follow_redirects,
        )

    # ── WebSocket ────────────────────────────────────────────────────

    def websocket_connect(
        self,
        path: str,
        subprotocols: list[str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _WebSocketSession:
        """Open an in-memory WebSocket against the app — context manager.

        Drives the ASGI websocket protocol: synthesise the scope, send
        `websocket.connect`, route to the handler, then forward
        `send_text` / `receive_text` / `close` calls to the running
        handler through a pair of asyncio queues.
        """
        return _WebSocketSession(self, path, subprotocols, headers)

    # ── Lifecycle ────────────────────────────────────────────────────

    def close(self) -> None:
        """Run shutdown lifecycle and close the loop if we own it."""
        if self._lifespan_run and hasattr(self.app, "_run_lifecycle"):
            self._loop.run_until_complete(self.app._run_lifecycle("shutdown"))
            self._lifespan_run = False
        if self._owns_loop and not self._loop.is_closed():
            self._loop.close()

    def __enter__(self) -> TestClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        if self._owns_loop and not self._loop.is_closed():
            with contextlib.suppress(Exception):
                self._loop.close()


class _WebSocketSession:
    """Sync wrapper around an in-memory ASGI websocket session.

    The handler runs as a background task on the TestClient's loop.
    Calls to `send_text`/`receive_text`/`close` route through two
    asyncio queues that play the role the network would normally play
    — the client-side `send_text` enqueues `websocket.receive` for the
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
            for k, v in self._headers.items():
                scope_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))

            # Split any `?query` off the connect path, mirroring
            # `_make_request` for HTTP. A real ASGI server does this; the
            # in-memory client must too, or route matching sees the `?`
            # and `WebSocket.query_params` stays empty.
            ws_path, _, ws_query = self._path.partition("?")

            scope = {
                "type": "websocket",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "scheme": "ws",
                "path": ws_path,
                "raw_path": ws_path.encode("utf-8"),
                "query_string": ws_query.encode("ascii"),
                "root_path": "",
                "headers": scope_headers,
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
                "subprotocols": list(self._subprotocols),
            }

            async def receive() -> dict:
                return await self._to_handler.get()

            async def send(msg: dict) -> None:
                await self._from_handler.put(msg)

            # Kick the handshake.
            await self._to_handler.put({"type": "websocket.connect"})
            self._handler_task = asyncio.create_task(self._client.app(scope, receive, send))

            # Wait for the accept (or close) frame before returning.
            first = await self._from_handler.get()
            if first["type"] == "websocket.close":
                code = first.get("code", 1000)
                raise RuntimeError(f"WebSocket rejected with close code {code}")
            if first["type"] != "websocket.accept":
                raise RuntimeError(
                    f"WebSocket handshake produced an unexpected ASGI message: {first['type']!r}"
                )
            self.accepted_subprotocol = first.get("subprotocol")

        self._client._loop.run_until_complete(_start())
        return self

    def __exit__(self, *exc: Any) -> None:
        async def _close() -> None:
            if self._handler_task and not self._handler_task.done():
                await self._to_handler.put({"type": "websocket.disconnect", "code": 1000})
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._handler_task, timeout=1.0)

        self._client._loop.run_until_complete(_close())

    def send_text(self, data: str) -> None:
        self._client._loop.run_until_complete(
            self._to_handler.put({"type": "websocket.receive", "text": data})
        )

    def send_bytes(self, data: bytes) -> None:
        self._client._loop.run_until_complete(
            self._to_handler.put({"type": "websocket.receive", "bytes": data})
        )

    def receive_text(self) -> str:
        async def _r() -> str:
            msg = await self._from_handler.get()
            if msg.get("type") == "websocket.close":
                raise Exception(f"WebSocket closed: {msg.get('code', 1000)}")
            return msg["text"]

        return self._client._loop.run_until_complete(_r())

    def receive_bytes(self) -> bytes:
        async def _r() -> bytes:
            msg = await self._from_handler.get()
            if msg.get("type") == "websocket.close":
                raise Exception(f"WebSocket closed: {msg.get('code', 1000)}")
            return msg["bytes"]

        return self._client._loop.run_until_complete(_r())

    def receive_json(self) -> Any:
        return orjson.loads(self.receive_text())

    def send_json(self, data: Any) -> None:
        self.send_text(orjson.dumps(data).decode("utf-8"))


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


class AsyncTestClient:
    """Async in-memory test client — drives the app through its ASGI surface.

    The async counterpart of `TestClient`: used as an async context
    manager inside an async test, so each request is `await`ed on the
    test's own running event loop instead of through a private loop. The
    request methods (`get` / `post` / …) are coroutines.

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
        base_url: str = "http://testserver",
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

    # ── Async context manager ────────────────────────────────────────

    async def __aenter__(self) -> AsyncTestClient:
        self._entered = True
        if hasattr(self.app, "_run_lifecycle"):
            await self.app._run_lifecycle("startup")
            self._lifespan_run = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._lifespan_run and hasattr(self.app, "_run_lifecycle"):
            await self.app._run_lifecycle("shutdown")
            self._lifespan_run = False
        self._entered = False

    # ── Cookie management ────────────────────────────────────────────

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

    # ── Header / cookie plumbing ─────────────────────────────────────

    def _build_headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        return _build_request_headers(self._base_headers, self._cookies, extra)

    def _update_cookies(self, response: TestResponse) -> None:
        _apply_set_cookie_to_jar(self._cookies, response.raw_headers)

    # ── ASGI dispatch ────────────────────────────────────────────────

    async def _send_one_request(
        self,
        method: str,
        path: str,
        query_string: str,
        headers: dict[str, str],
        body: bytes,
    ) -> TestResponse:
        scope = _build_scope(method, path, query_string, headers)

        body_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if body_sent:
                return {"type": "http.disconnect"}
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        status_code = 500
        raw_headers: list[tuple[bytes, bytes]] = []
        body_chunks: list[bytes] = []

        async def send(message: dict[str, Any]) -> None:
            nonlocal status_code, raw_headers
            mtype = message["type"]
            if mtype == "http.response.start":
                status_code = message["status"]
                raw_headers = list(message.get("headers") or [])
            elif mtype == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    body_chunks.append(chunk)

        await self.app(scope, receive, send)
        return TestResponse(status_code, b"".join(body_chunks), raw_headers)

    async def _make_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        query_string: str = "",
        follow_redirects: bool | None = None,
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
        current_method, current_path = method, path
        current_query, current_body = query_string, body
        current_headers = headers

        for _ in range(self._MAX_REDIRECTS + 1):
            all_headers = self._build_headers(current_headers)
            resp = await self._send_one_request(
                current_method, current_path, current_query, all_headers, current_body
            )
            self._update_cookies(resp)
            if not follow or resp.status_code not in (301, 302, 303, 307, 308):
                return resp
            location = resp.headers.get("location") or resp.headers.get("Location")
            if not location:
                return resp
            # 301/302/303 → GET with no body (browser convention);
            # 307/308 preserve method + body.
            if resp.status_code in (301, 302, 303):
                current_method, current_body = "GET", b""
            current_path, current_query = _resolve_redirect_location(location, self.base_url)
            # `current_headers` is intentionally kept across the hop — the
            # caller's headers (Authorization, custom headers) must reach
            # the redirected request, matching the sync TestClient.
        raise RuntimeError(
            f"AsyncTestClient exceeded {self._MAX_REDIRECTS} redirects following {method} {path}"
        )

    def _assemble_body(
        self,
        json: Any,
        data: dict[str, str] | None,
        content: bytes | None,
        files: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> tuple[bytes, dict[str, str]]:
        hdrs = dict(headers or {})
        body = b""
        if files is not None:
            body, ct = _encode_multipart(files, data or {})
            hdrs["content-type"] = ct
        elif json is not None:
            body = orjson.dumps(json)
            hdrs.setdefault("content-type", "application/json")
        elif data is not None:
            body = urlencode(data).encode()
            hdrs.setdefault("content-type", "application/x-www-form-urlencoded")
        elif content is not None:
            body = content
        return body, hdrs

    # ── Method shortcuts ─────────────────────────────────────────────

    async def get(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        qs = urlencode(params) if params else ""
        return await self._make_request(
            "GET", path, headers=headers, query_string=qs, follow_redirects=follow_redirects
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
    ) -> TestResponse:
        body, hdrs = self._assemble_body(json, data, content, files, headers)
        return await self._make_request(
            "POST", path, headers=hdrs, body=body, follow_redirects=follow_redirects
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
    ) -> TestResponse:
        body, hdrs = self._assemble_body(json, data, content, files, headers)
        return await self._make_request(
            "PUT", path, headers=hdrs, body=body, follow_redirects=follow_redirects
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
    ) -> TestResponse:
        body, hdrs = self._assemble_body(json, data, content, files, headers)
        return await self._make_request(
            "PATCH", path, headers=hdrs, body=body, follow_redirects=follow_redirects
        )

    async def delete(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        return await self._make_request(
            "DELETE", path, headers=headers, follow_redirects=follow_redirects
        )

    async def head(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        return await self._make_request(
            "HEAD", path, headers=headers, follow_redirects=follow_redirects
        )

    async def options(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> TestResponse:
        return await self._make_request(
            "OPTIONS", path, headers=headers, follow_redirects=follow_redirects
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
    ) -> TestResponse:
        """Generic verb-agnostic request dispatcher (see `TestClient.request`)."""
        verb = method.upper()
        if json is not None or data is not None or content is not None or files is not None:
            body, hdrs = self._assemble_body(json, data, content, files, headers)
            return await self._make_request(
                verb, path, headers=hdrs, body=body, follow_redirects=follow_redirects
            )
        qs = urlencode(params) if params else ""
        return await self._make_request(
            verb, path, headers=headers, query_string=qs, follow_redirects=follow_redirects
        )

    def __repr__(self) -> str:
        return f"<AsyncTestClient app={self.app!r}>"
