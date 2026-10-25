"""Testing support — test-client and outside-request context factories, mixed into Veloce.

Every method is a constructor call, off the request path, importing its class
lazily so `import veloce` pulls neither the test client nor the context machinery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from veloce._protocol_constants import HTTP_METHOD_GET

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app.contexts import _AppContext, _TestRequestContext
    from veloce.app.core import Veloce
    from veloce.testclient import AsyncTestClient, TestClient


class TestingMixin:
    """In-memory test-client and outside-request context factories."""

    def test_client(self, **kwargs: Any) -> TestClient:
        """Return an in-memory `TestClient` for this app.

        Kwargs are forwarded to `TestClient.__init__`.
        """
        from veloce.testclient import TestClient

        return TestClient(self, **kwargs)

    def async_test_client(self, **kwargs: Any) -> AsyncTestClient:
        """Return an `AsyncTestClient` for this app.

        Requests are awaited on the test's own running loop rather than driven
        through a private one. Kwargs are forwarded to `AsyncTestClient.__init__`.
        """
        from veloce.testclient import AsyncTestClient

        return AsyncTestClient(self, **kwargs)

    def app_context(self) -> _AppContext:
        """Bind `current_app` and reset `g` for use outside a request.

        Nestable: the previous binding, if any, is restored on exit.
        """
        from veloce.app.contexts import _AppContext

        return _AppContext(cast("Veloce", self))

    def test_request_context(
        self,
        path: str = "/",
        method: str = HTTP_METHOD_GET,
        headers: dict[str, str] | None = None,
        query_string: str = "",
        body: bytes = b"",
    ) -> _TestRequestContext:
        """Synthesise a fake request for outside-request testing.

        A strict subset of what `handle_request` does: no middleware, no DI,
        no handler.
        """
        from veloce.app.contexts import _TestRequestContext

        return _TestRequestContext(
            cast("Veloce", self),
            path=path,
            method=method,
            headers=headers or {},
            query_string=query_string,
            body=body,
        )
