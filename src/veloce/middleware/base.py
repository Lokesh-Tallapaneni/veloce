"""Base middleware classes.

Two shapes are first-class:

- `Middleware`: split request/response hooks. Veloce-native shape, lightweight.
- `BaseHTTPMiddleware`: a single `dispatch(request, call_next)` coroutine that
  wraps the inner handler — a common ASGI pattern. Useful when the
  middleware needs to inspect the response after computing the request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from veloce.http.request import Request
from veloce.http.response import Response


class Middleware:
    """Base middleware class. Subclass and override process_request/process_response."""

    async def process_request(self, request: Request) -> Response | None:
        """Called before route handler. Return a Response to short-circuit."""
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Called after route handler. Can modify the response."""
        return response


# Type alias for the `call_next` argument so user dispatch functions can
# annotate it.
CallNext = Callable[[Request], Awaitable[Response]]


class BaseHTTPMiddleware:
    """Class-based dispatch-shape middleware.

    Subclass and override `dispatch`:

        class TimingMW(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                start = time.perf_counter()
                response = await call_next(request)
                response.headers["X-Elapsed-ms"] = str(
                    int((time.perf_counter() - start) * 1000)
                )
                return response

        app.add_http_middleware(TimingMW())

    For one-off middleware, construct with `dispatch=fn` instead of
    subclassing:

        async def my_dispatch(request, call_next): ...
        app.add_http_middleware(BaseHTTPMiddleware(dispatch=my_dispatch))

    The instance is callable as `(request, call_next) -> response`, so it
    composes with the existing `@app.middleware("http")` chain.
    """

    def __init__(self, dispatch: Callable | None = None) -> None:
        if dispatch is not None:
            # Bind the supplied callable as the dispatch method. Note that
            # bare-function bindings don't get an automatic `self` — we call
            # them with (request, call_next) directly via `__call__`.
            self._dispatch_override: Callable | None = dispatch
        else:
            self._dispatch_override = None

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        """Override this in subclasses. Default just calls through.

        Implementations must await `call_next(request)` exactly once to
        reach the wrapped handler.
        """
        return await call_next(request)

    async def __call__(self, request: Request, call_next: CallNext) -> Response:
        if self._dispatch_override is not None:
            return await self._dispatch_override(request, call_next)
        return await self.dispatch(request, call_next)
