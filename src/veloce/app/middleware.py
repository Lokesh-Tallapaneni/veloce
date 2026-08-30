"""Middleware registration — the three registration shapes, mixed into Veloce.

Holds the registration funnel for the three middleware shapes Veloce accepts:
request/response `Middleware` instances and subclasses (with priority ordering),
standard ASGI middleware classes (wrapped when the ASGI stack is assembled), and
`BaseHTTPMiddleware`-style `(request, call_next)` callables. A mixin on `Veloce`;
everything here is setup-only and the ordered chain is resolved once at
registration, so per-request dispatch pays no sorting cost.

This is the app-side registration surface; the middleware base classes and the
built-in middleware live in the separate `veloce.middleware` package.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from veloce.app._host import AppHost
from veloce.middleware import BaseHTTPMiddleware, Middleware


class MiddlewareMixin(AppHost):
    """Middleware registration funnel, mixed into `Veloce`."""

    @property
    def middlewares(self) -> tuple[Middleware, ...]:
        """The registered `Middleware` instances, in the order they will run.

        Registration order unless priorities were set, in which case this is the
        priority order the pipeline actually uses. Checking whether a middleware
        is installed - a plugin guarding against registering itself twice, or a
        startup check that CORS is present - otherwise means reading a private
        list.

        Standard ASGI middleware classes are not here; they are wrapped around
        the app when the ASGI stack is assembled rather than run per request by
        this pipeline.

        Usage::

            if not any(isinstance(m, CORSMiddleware) for m in app.middlewares):
                app.add_middleware(CORSMiddleware, allow_origins=["*"])
        """
        return tuple(self._middlewares)

    def add_middleware(self, middleware: Any, **options: Any) -> None:
        """Add middleware to the pipeline.

        Call forms:

        - `add_middleware(VeloceMiddlewareClass, **options)` - a class
          subclassing `Middleware` is instantiated with `**options` and
          appended to the request/response pipeline.
        - `add_middleware(instance)` - append an already-built `Middleware`
          instance directly.
        - `add_middleware(ASGIMiddlewareClass, **options)` - a class that
          is *not* a `Middleware` subclass is treated as a standard ASGI
          middleware: it wraps the whole application and is instantiated
          as `ASGIMiddlewareClass(app, **options)` when the ASGI stack is
          assembled. This is what lets third-party ASGI middleware
          (observability, tracing, profiling, ...) plug in. Middleware
          added first is the outermost wrapper.

        Pass `name=` to override the instance's exclusion name (the identifier
        `exclude_middleware=[...]` on a route references). The override is
        applied *after* construction rather than forwarded into the subclass
        constructor, so per-instance naming works for every `Middleware`
        subclass - including user subclasses whose `__init__` does not accept a
        `name` keyword.

        Pass `priority=` (an `int`, default `0`) to order this middleware
        deterministically regardless of registration order. Higher priority
        runs earlier in the request phase and correspondingly later in the
        response phase; middleware of equal priority keeps registration order
        (a stable tiebreak). The ordered chain is resolved once at registration
        time, so per-request dispatch pays no sorting cost. When no middleware
        sets a priority the behaviour is unchanged - the chain is the plain
        registration order it has always been. `priority` applies to the
        request/response `Middleware` pipeline only, not to ASGI-class
        middleware (which is ordered by its own wrap nesting).
        """
        self._assert_mutable()
        # `priority` is a framework ordering concern, not a construction
        # argument: pop it before any middleware is built so it is never
        # forwarded into a `Middleware` subclass constructor.
        priority = options.pop("priority", 0)
        if isinstance(middleware, type):
            if issubclass(middleware, Middleware):
                # The name override is a framework concern, not a construction
                # argument: pop it so arbitrary subclass constructors (which may
                # not accept `name`) build cleanly, then stamp it on the built
                # instance via the base `name` attribute the exclusion lookup
                # reads.
                name = options.pop("name", None)
                instance = middleware(**options)
                if name is not None:
                    instance.name = name
                self._register_middleware(instance, priority)
            elif issubclass(middleware, BaseHTTPMiddleware):
                # `BaseHTTPMiddleware` is a dispatch-shape middleware, not
                # an ASGI app - registering it as ASGI would wire the app
                # in as its `dispatch` and fail at request time.
                raise TypeError(
                    f"{middleware.__name__} is a BaseHTTPMiddleware "
                    "(dispatch-shape) - register it with add_http_middleware(), "
                    "not add_middleware()."
                )
            else:
                # A standard ASGI middleware class - it needs the app it
                # wraps, so defer construction until the stack is built. Bumping
                # the generation counter invalidates the compiled wrap slot and,
                # through the gen-keyed stack cache, the assembled stack too - no
                # separate `_asgi_stack` reset needed.
                self._asgi_middleware.append((middleware, options))
                self._gen += 1
        elif isinstance(middleware, Middleware):
            # An already-built instance takes no construction arguments, so the
            # only option it can honour is the name override - and it must, or
            # `exclude_middleware=[name]` silently matches nothing and a route
            # keeps a middleware the author believes they opted out of.
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
            # A bare ASGI middleware instance cannot be wired up - veloce
            # has to supply the wrapped app, which only the class form
            # allows.
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
        # Class -> instance.
        if isinstance(middleware, type):
            middleware = middleware()
        if not callable(middleware):
            raise TypeError(
                f"add_http_middleware expects a callable / instance / class, got {middleware!r}"
            )
        self._register_feature_state(self._http_middleware_funcs, middleware)
        return middleware

    def _register_middleware(self, instance: Middleware, priority: int) -> None:
        """Record a built `Middleware` instance and refresh the ordered chain.

        Appends `(priority, sequence, instance)` to the registration ledger and
        bumps the middleware generation counter (so per-route exclusion chains
        recompute). While every registered priority is `0` the ordered chain is
        just the append-order list, so `_middlewares` is updated by a plain
        append and no sort runs - keeping the common no-priority case identical
        to the previous behaviour. The first non-zero priority flips the app
        into ordered mode and rebuilds `_middlewares` as a stable descending
        sort by priority. All of this happens at registration time, never per
        request.
        """
        seq = self._middleware_seq
        self._middleware_seq = seq + 1
        self._middleware_records.append((priority, seq, instance))
        self._mw_version += 1
        # The middleware set drives the WS-handshake phase, so the middleware
        # ledger funnel doubles as a pipeline-invalidation sink.
        self._gen += 1
        if priority and not self._any_priority:
            self._any_priority = True
        if self._any_priority:
            # Stable sort by descending priority: Python's sort is stable, so
            # within an equal priority the registration `seq` order is kept.
            ordered = sorted(self._middleware_records, key=lambda r: -r[0])
            self._middlewares = [rec[2] for rec in ordered]
        else:
            self._middlewares.append(instance)

    def middleware(
        self, middleware_class_or_type: type | str, **kwargs: Any
    ) -> Callable[[Callable], Callable] | None:
        """Add middleware - supports both a class form and a decorator form.

        The two forms return different things: the decorator form returns the
        decorator, and the **class form returns `None`** because it is a
        statement, not a decorator. Writing `@app.middleware(CORSMiddleware)`
        therefore fails at decoration rather than silently doing nothing.

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
                # The decorator form registers a plain function and has nowhere to
                # put a class-form option. Dropping them silently is the trap: the
                # two most plausible - `priority=` and `name=` - are real
                # `add_middleware` options, so an author gets unordered or unnamed
                # middleware and nothing says so.
                names = ", ".join(sorted(kwargs))
                raise TypeError(
                    f"@app.middleware('http') takes no options; got {names}. "
                    f"Pass them to the class form, app.middleware(MyMiddleware, ...), "
                    f"or to app.add_middleware(...)."
                )

            def decorator(func: Callable) -> Callable:
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
            # Explicit: the class form is a statement, and the annotation says
            # so, so the `None` is written rather than left implicit.
            return None
