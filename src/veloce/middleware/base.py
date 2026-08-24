"""Middleware base classes — the two first-class middleware shapes.

- `Middleware`: split request/response hooks. Veloce-native shape, lightweight.
- `BaseHTTPMiddleware`: a single `dispatch(request, call_next)` coroutine that
  wraps the inner handler — a common ASGI pattern. Useful when the
  middleware needs to inspect the response after computing the request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, ClassVar

from veloce.http.request import Request
from veloce.http.response import Response

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

    from veloce.audit import AuditContext, Finding

# The `call_next` argument type, so user dispatch functions can annotate it.
CallNext = Callable[[Request], Awaitable[Response]]


class Auditable:
    """What a registered component declares about its own security posture.

    Mixed into every middleware shape rather than owned by one of them: Veloce
    accepts `Middleware` instances, `BaseHTTPMiddleware` dispatch objects and
    ASGI middleware classes, and the audit used to walk only the first. A
    dispatch-shape middleware that hardened every response was reported as
    absent, because it had no way to say otherwise.
    """

    __slots__ = ()

    # Set True by a component that adds hardening headers to every response.
    # The audit warns when nothing in the stack claims this, and asks the marker
    # rather than naming a class so a middleware outside this package answers
    # the question too.
    sets_hardening_headers: ClassVar[bool] = False

    # Set True when `audit` reads the route table. The audit then skips this
    # component until the table is final, so `veloce check` - which imports the
    # app without starting it - cannot report a route as missing when it is
    # merely registered later, during startup.
    audit_needs_routes: ClassVar[bool] = False

    def audit(self, ctx: AuditContext) -> Iterable[Finding]:
        """Report what is wrong with this component's own configuration.

        The audit collects these from everything registered, so a check belongs
        to the thing it is about and an app that registers none never loads the
        code that checks them. Return nothing when there is nothing to say.

        Severity decides what a finding does: an `error` refuses the boot, a
        `warning` fails `veloce check` without stopping anything, and `info`
        fails nothing. Set `audit_needs_routes` when the check reads
        `ctx.app`'s routes. Runs at audit and startup time only - never on a
        request path.
        """
        return ()


class Middleware(Auditable):
    """Base middleware class. Subclass and override process_request/process_response.

    Each middleware carries a `name` used by per-route exclusion
    (`exclude_middleware=[...]` on a route). The default name is the
    concrete class name; override the class attribute, or pass `name=` when
    two instances of the same class must be addressed independently.
    """

    # Identifier a route references to opt out of this middleware. Defaults
    # to the class name; a per-instance override is honoured by `__init__`.
    name: str = ""

    def __init__(self, *, name: str | None = None) -> None:
        if name is not None:
            self.name = name

    @property
    def middleware_name(self) -> str:
        """Resolved exclusion name - the instance/class `name` or class name."""
        return self.name or type(self).__name__

    async def process_request(self, request: Request) -> Response | None:
        """Called before route handler. Return a Response to short-circuit."""
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Called after route handler. Can modify the response."""
        return response

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"


class BaseHTTPMiddleware(Auditable):
    """Class-based dispatch-shape middleware.

    Subclass and override `dispatch`, or construct with `dispatch=fn` for a
    one-off middleware. The instance is callable as
    `(request, call_next) -> response`, so it composes with the existing
    `@app.middleware("http")` chain.

    Usage::

        class TimingMW(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                start = time.perf_counter()
                response = await call_next(request)
                response.headers["X-Elapsed-ms"] = str(
                    int((time.perf_counter() - start) * 1000)
                )
                return response

        app.add_http_middleware(TimingMW())

        # Or, without subclassing:
        async def my_dispatch(request, call_next): ...
        app.add_http_middleware(BaseHTTPMiddleware(dispatch=my_dispatch))
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
