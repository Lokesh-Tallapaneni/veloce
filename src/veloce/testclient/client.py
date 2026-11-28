"""The synchronous `TestClient`."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import (
    Generator,
    Sequence,
)
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from veloce._protocol_constants import (
    HTTP_METHOD_DELETE,
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
    HTTP_METHOD_PATCH,
    HTTP_METHOD_POST,
    HTTP_METHOD_PUT,
    LIFECYCLE_SHUTDOWN,
    LIFECYCLE_STARTUP,
    URL_SCHEME_HTTP,
)

if TYPE_CHECKING:  # pragma: no cover
    from veloce.sessions import Session
from veloce.testclient._asgi import _new_loop, _send_one_request
from veloce.testclient._body import StreamBody, _prepare_body_request
from veloce.testclient._cookies import _apply_set_cookie_to_jar, _TestClientCookies
from veloce.testclient._headers import _build_request_headers, _HeadersArg
from veloce.testclient._redirects import _follow_redirects
from veloce.testclient._ws_session import _WebSocketSession
from veloce.testclient.response import TestResponse


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
        *,
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
        """Add or update a cookie sent on every subsequent request.

        Args:
            key: Cookie name.
            value: Cookie value.

        """
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
        *,
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
        *,
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
        *,
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
        *,
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
        *,
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
        *,
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
        *,
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
        *,
        follow_redirects: bool | None = None,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        """Dispatch a request of any verb - httpx/test-client shape.

        The verb-agnostic form of `client.get` / `client.post` / .... Bodies and
        `params` are handled exactly as the per-verb methods do, and `stream`
        excludes the buffered body forms.

        Args:
            method: HTTP verb, case-insensitive.
            path: Request path.
            json: Body serialised as JSON.
            data: Body sent as a urlencoded form.
            headers: Extra request headers.
            content: Raw request body bytes.
            files: Files to upload as a multipart form.
            params: Query parameters appended to the path.
            follow_redirects: Follow redirects; `None` uses the client default.
            stream: Body fed as chunks; excludes `json`, `data`, `content` and `files`.

        Returns:
            The response.

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

        Args:
            path: The websocket path.
            subprotocols: Subprotocols offered to the app.
            headers: Extra handshake headers.

        Returns:
            A context manager yielding the session.

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
