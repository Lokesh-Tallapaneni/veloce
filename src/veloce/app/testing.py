"""Testing support — test-client and outside-request context factories, mixed into Veloce.

Holds the thin factory methods that build the in-memory test clients
(`test_client` / `async_test_client`) and the outside-request context managers
(`app_context` / `test_request_context`). A mixin on `Veloce`; every method is a
constructor call, used only from tests and CLI/background code, never on the
request path. The classes they build live in `veloce.testclient` and
`veloce.app.contexts` and are imported lazily inside each method so importing
`Veloce` does not pull the test client or the context machinery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from veloce._protocol_constants import HTTP_METHOD_GET

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app.contexts import _AppContext, _TestRequestContext
    from veloce.app.core import Veloce


class TestingMixin:
    """In-memory test-client and outside-request context factories."""

    def test_client(self, **kwargs: Any) -> Any:
        """Return an in-memory `TestClient` for this app.

        `app.test_client()` is the factory API; the kwargs (e.g.
        `follow_redirects=True`, `base_url=...`) are forwarded to
        `TestClient.__init__`. Equivalent to `TestClient(app, **kwargs)`
        for callers that prefer the method form.
        """
        from veloce.testclient import TestClient

        return TestClient(self, **kwargs)

    def async_test_client(self, **kwargs: Any) -> Any:
        """Return an `AsyncTestClient` for this app.

        The async counterpart of `test_client()` - used as
        `async with app.async_test_client() as client:` inside an async
        test, so requests are awaited on the test's own running loop
        rather than driven through a private loop. Kwargs are forwarded
        to `AsyncTestClient.__init__`.
        """
        from veloce.testclient import AsyncTestClient

        return AsyncTestClient(self, **kwargs)

    def app_context(self) -> _AppContext:
        """Bind `current_app` and reset `g` for use outside a request.

        Use as `with app.app_context(): ...`. CLI commands, background
        jobs, and tests need this when they want to read `app.config` or
        write into `g` without going through `handle_request`. Nestable:
        the previous binding (if any) is restored on exit.
        """
        # Lazy for the reason the module docstring gives - keeping the context
        # machinery off `import veloce` - not for a cycle: nothing under
        # `veloce.http` imports `veloce.app`, and hoisting this imports cleanly.
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

        Inside `with app.test_request_context(): ...`, `current_app`, `g`,
        and the request-scoped contextvars resolve as if Veloce
        had just received that request - without spinning up the full
        dispatch pipeline. Strict subset of what `handle_request` does:
        no middleware, no DI, no handler.
        """
        # Lazy for the reason the module docstring gives - keeping the context
        # machinery off `import veloce` - not for a cycle: hoisting it was
        # measured to import cleanly in every ordering.
        from veloce.app.contexts import _TestRequestContext

        return _TestRequestContext(
            cast("Veloce", self),
            path=path,
            method=method,
            headers=headers or {},
            query_string=query_string,
            body=body,
        )
