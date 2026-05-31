"""Middleware base classes — the two first-class middleware shapes.

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

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"


# The `call_next` argument type, so user dispatch functions can annotate it.
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
        # A supplied callable is invoked with (request, call_next) directly via
        # `__call__`; bare-function bindings don't receive an automatic `self`.
        self._dispatch_override: Callable | None = dispatch

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        """Override this in subclasses. Default just calls through.

        Implementations must await `call_next(request)` exactly once to
        reach the wrapped handler.
        """
        return await call_next(request)

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    async def __call__(self, request: Request, call_next: CallNext) -> Response:
        if self._dispatch_override is not None:
            return await self._dispatch_override(request, call_next)
        return await self.dispatch(request, call_next)
