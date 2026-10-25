"""Middleware registration — the three registration shapes, mixed into Veloce.

The registration funnel for the three shapes Veloce accepts: request/response
`Middleware` instances and subclasses (with priority ordering), standard ASGI
middleware classes (wrapped when the ASGI stack is assembled), and
`BaseHTTPMiddleware`-style `(request, call_next)` callables. Setup-only, with
the ordered chain resolved once at registration so dispatch pays no sorting
cost. The base classes and built-in middleware live in `veloce.middleware`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from typing_extensions import Doc

from veloce.app._host import AppHost
from veloce.middleware import BaseHTTPMiddleware, Middleware


class MiddlewareMixin(AppHost):
    """Middleware registration funnel, mixed into `Veloce`."""

    @property
    def middlewares(self) -> tuple[Middleware, ...]:
        """The registered `Middleware` instances, in the order they will run.

        Registration order unless priorities were set, in which case this is the
        priority order the pipeline uses. Standard ASGI middleware classes are
        not here; they wrap the app when the ASGI stack is assembled.

        Usage::

            if not any(isinstance(m, CORSMiddleware) for m in app.middlewares):
                app.add_middleware(CORSMiddleware, allow_origins=["*"])
        """
        return tuple(self._middlewares)

    def add_middleware(
        self,
        middleware: Annotated[
            Any, Doc("A `Middleware` instance, a middleware class, or an ASGI wrapper class.")
        ],
        **options: Annotated[Any, Doc("Keyword arguments the middleware class is built with.")],
    ) -> None:
        """Add middleware to the pipeline.

        Call forms:

        - `add_middleware(VeloceMiddlewareClass, **options)` - a class
          subclassing `Middleware` is instantiated with `**options` and
          appended to the request/response pipeline.
        - `add_middleware(instance)` - append an already-built `Middleware`
          instance directly.
        - `add_middleware(ASGIMiddlewareClass, **options)` - a class that is
          *not* a `Middleware` subclass is treated as standard ASGI middleware,
          instantiated as `ASGIMiddlewareClass(app, **options)` when the stack
          is assembled. Middleware added first is the outermost wrapper.

        Pass `name=` to override the instance's exclusion name (the identifier
        `exclude_middleware=[...]` on a route references). It is applied *after*
        construction, so per-instance naming works even for a subclass whose
        `__init__` does not accept a `name` keyword.

        Pass `priority=` (an `int`, default `0`) to order this middleware
        regardless of registration order. Higher priority runs earlier in the
        request phase and later in the response phase, with equal priorities
        keeping registration order. It applies to the request/response pipeline
        only, not to ASGI-class middleware, which its wrap nesting orders.
        """
        self._assert_mutable()
        # Popped before anything is built, so it never reaches a constructor.
        priority = options.pop("priority", 0)
        if isinstance(middleware, type):
            if issubclass(middleware, Middleware):
                # Popped so a constructor without a `name` keyword still builds.
                name = options.pop("name", None)
                instance = middleware(**options)
                if name is not None:
                    instance.name = name
                self._register_middleware(instance, priority)
            elif issubclass(middleware, BaseHTTPMiddleware):
                # As ASGI this would wire the app in as its `dispatch`.
                raise TypeError(
                    f"{middleware.__name__} is a BaseHTTPMiddleware "
                    "(dispatch-shape) - register it with add_http_middleware(), "
                    "not add_middleware()."
                )
            else:
                # It needs the app it wraps, so construction waits for the
                # stack; the bump invalidates the compiled wrap slot.
                self._asgi_middleware.append((middleware, options))
                self._gen += 1
        elif isinstance(middleware, Middleware):
            # The only option a built instance can honour - and must, or
            # `exclude_middleware=[name]` silently matches nothing.
            name = options.pop("name", None)
            if name is not None:
                middleware.name = name
            if options:
                raise TypeError(
                    f"add_middleware() received {', '.join(sorted(options))} for an "
                    "already-built middleware instance; construction arguments must "
                    "be passed to the constructor, or pass the class instead."
                )
            self._register_middleware(middleware, priority)
        else:
            # Only the class form lets veloce supply the wrapped app.
            raise TypeError(
                f"add_middleware() received a {type(middleware).__name__} instance; "
                "pass a Middleware instance, a Middleware subclass, or an ASGI "
                "middleware *class* (so veloce can supply the wrapped app). "
                "Register a BaseHTTPMiddleware via add_http_middleware()."
            )

    def add_http_middleware(self, middleware: Any) -> Any:
        """Register a middleware on the `(request, call_next) -> response` chain.

        Accepts a `BaseHTTPMiddleware` instance, a bare callable, or a class
        (which is instantiated with no args). Returns the registered object so
        it can be used as a decorator.
        """
        if isinstance(middleware, type):
            middleware = middleware()
        if not callable(middleware):
            raise TypeError(
                f"add_http_middleware expects a callable / instance / class, got {middleware!r}"
            )
        self._register_feature_state(self._http_middleware_funcs, middleware)
        return middleware

    def middleware(
        self, middleware_class_or_type: type[Any] | str, **kwargs: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]] | None:
        """Add middleware - supports both a class form and a decorator form.

        The decorator form returns the decorator; the **class form returns
        `None`**, so `@app.middleware(CORSMiddleware)` fails at decoration
        rather than silently doing nothing.

        Class form: app.middleware(CORSMiddleware, allow_origins=["*"])
        Decorator form:
            @app.middleware("http")
            async def add_header(request, call_next):
                response = await call_next(request)
                response.headers["X-Custom"] = "value"
                return response
        """
        if isinstance(middleware_class_or_type, str) and middleware_class_or_type == "http":
            if kwargs:
                # Nowhere to put a class-form option, so it would be dropped.
                names = ", ".join(sorted(kwargs))
                raise TypeError(
                    f"@app.middleware('http') takes no options; got {names}. "
                    f"Pass them to the class form, app.middleware(MyMiddleware, ...), "
                    f"or to app.add_middleware(...)."
                )

            def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
                self._register_feature_state(self._http_middleware_funcs, func)
                return func

            return decorator
        else:
            if isinstance(middleware_class_or_type, str):
                raise TypeError(
                    "middleware(middleware_class) is the class form; "
                    "@app.middleware('http') is the decorator form"
                )
            self.add_middleware(middleware_class_or_type, **kwargs)
            # Written out: the class form is a statement, as the annotation says.
            return None

    def _register_middleware(self, instance: Middleware, priority: int) -> None:
        """Record a built `Middleware` instance and refresh the ordered chain.

        While every registered priority is `0` the chain is the append-order
        list and no sort runs; the first non-zero priority flips the app into
        ordered mode and rebuilds it as a stable descending sort. Registration
        time only, never per request.
        """
        seq = self._middleware_seq
        self._middleware_seq = seq + 1
        self._middleware_records.append((priority, seq, instance))
        self._mw_version += 1
        # The middleware set drives the WS-handshake phase too.
        self._gen += 1
        if priority and not self._any_priority:
            self._any_priority = True
        if self._any_priority:
            # Python's sort is stable, so an equal priority keeps `seq` order.
            ordered = sorted(self._middleware_records, key=lambda r: -r[0])
            self._middlewares = [rec[2] for rec in ordered]
        else:
            self._middlewares.append(instance)
