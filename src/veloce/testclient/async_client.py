"""The `AsyncTestClient`, for tests that already run on an event loop."""

from __future__ import annotations

from collections.abc import (
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
    pass
from veloce.testclient._asgi import _send_one_request
from veloce.testclient._body import StreamBody, _prepare_body_request
from veloce.testclient._cookies import _apply_set_cookie_to_jar, _TestClientCookies
from veloce.testclient._headers import _build_request_headers, _HeadersArg
from veloce.testclient._redirects import _follow_redirects
from veloce.testclient.response import TestResponse


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
        *,
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
        *,
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
        *,
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
        *,
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
        *,
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
        *,
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
        *,
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
        *,
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
        *,
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
