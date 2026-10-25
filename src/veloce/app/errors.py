"""Exception handling — registration and dispatch mixed into Veloce.

Holds the error-handler registry (`exception_handler` / `register_error_handler`
/ `add_exception_handler`), the MRO-walked handler lookup, and the
`handle_http_exception` / `handle_user_exception` builders that the request
pipeline calls when a handler raises. A mixin on `Veloce`; only the error path
reaches here, never the per-request hot path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import veloce.status as status
from veloce._constants import (
    HEADER_ALLOW,
    MIME_TEXT_PLAIN,
    MSG_INTERNAL_SERVER_ERROR,
)
from veloce._internal import T_route, _coerce_bool
from veloce._protocol_constants import (
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
)
from veloce.app._host import AppHost
from veloce.exceptions import HTTPException, _error_handler_key_error, http_exception_payload
from veloce.http.request import Request
from veloce.http.response import (
    JSONResponse,
    Response,
)

# `None` is itself a valid cache hit ("no handler matched this type"), so a
# plain `cache.get(k)` would re-walk the MRO for every unhandled type.
_MISSING: Any = object()


class ErrorsMixin(AppHost):
    """Exception-handler registration and dispatch, mixed into `Veloce`."""

    def register_error_handler(
        self, code_or_exception: int | type[Any], func: Callable[..., Any]
    ) -> None:
        """Register an error handler without a decorator.

        The key is an `int` status code or an exception class. Anything else is
        refused: `_exception_handlers` is matched by walking a raised exception's
        MRO, so a non-class key could never be found and would register silently.
        """
        self._assert_mutable()
        if isinstance(code_or_exception, int):
            self._status_handlers[code_or_exception] = func
        else:
            if not (
                isinstance(code_or_exception, type) and issubclass(code_or_exception, BaseException)
            ):
                raise TypeError(_error_handler_key_error(code_or_exception))
            self._exception_handlers[code_or_exception] = func
            # Invalidated on any registration so a new base-class handler takes
            # effect for the already-cached subclasses.
            self._exc_handler_cache.clear()

    def exception_handler(
        self, exc_class_or_status: type[Any] | int
    ) -> Callable[[T_route], T_route]:
        """Register a custom exception handler by exception type or status code."""

        def decorator(func: T_route) -> T_route:
            self.register_error_handler(exc_class_or_status, func)
            return func

        return decorator

    # An alias, not a second implementation - semantics are identical.
    errorhandler = exception_handler

    def add_exception_handler(
        self, exc_class_or_status: type[Any] | int, handler: Callable[..., Any]
    ) -> None:
        """Imperative exception-handler registration - ASGI shape.

        The non-decorator form of `@app.exception_handler(...)`.
        Accepts an exception class (matched by MRO at dispatch time) or
        an int HTTP status code.
        """
        self.register_error_handler(exc_class_or_status, handler)

    def log_exception(self, exc: BaseException, request: Request | None = None) -> None:
        """Log an exception with traceback.

        Routes the exception through the app's own logger at ERROR level, so an
        operator silences it with ordinary logging configuration. Pass `request`
        to name the request that failed; a background task or CLI hook omits it.
        """
        if request is not None:
            self.logger.error("Exception on %s [%s]", request.path, request.method, exc_info=exc)
        else:
            self.logger.error("Exception on request", exc_info=exc)

    def make_default_options_response(
        self, path: str, allowed_methods: list[str] | None = None
    ) -> Response:
        """Build the auto-OPTIONS response for `path`.

        Returns a 200 response with an empty body and an `Allow` header listing
        every method registered for `path`, augmented with `HEAD` (whenever
        `GET` is supported) and `OPTIONS` itself per RFC 9110 Sec. 9.3.7. Pass
        `allowed_methods` when the registered set is already known.
        """
        allowed = allowed_methods if allowed_methods is not None else self.get_allowed_methods(path)
        advertised = list(allowed)
        if HTTP_METHOD_GET in advertised and HTTP_METHOD_HEAD not in advertised:
            advertised.append(HTTP_METHOD_HEAD)
        if HTTP_METHOD_OPTIONS not in advertised:
            advertised.append(HTTP_METHOD_OPTIONS)
        return Response(
            status_code=status.HTTP_200_OK,
            body=b"",
            content_type=MIME_TEXT_PLAIN,
            headers={HEADER_ALLOW: ", ".join(advertised)},
        )

    async def handle_http_exception(
        self, exc: HTTPException, request: Request | None = None
    ) -> Response:
        """Build the response for an `HTTPException`.

        Walks registered status-code + class handlers first (matching `abort()`
        semantics), falling back to JSON
        `{"detail": exc.detail, "status_code": exc.status_code}` with
        `exc.headers` applied - byte-identical to what the request cycle emits,
        so a handler reached over MCP reports the error as it does over HTTP.

        Pass `request=` from inside a request scope so the registered handler
        receives the real failing request instead of a synthetic `GET /`.
        """
        handler = self._find_scoped_status_handler(
            exc.status_code, request
        ) or self._find_scoped_exception_handler(type(exc), request)
        if handler is not None:
            return await self._run_exc_handler(handler, exc, request)
        return JSONResponse(
            http_exception_payload(exc),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    async def handle_user_exception(
        self, exc: BaseException, request: Request | None = None
    ) -> Response:
        """Dispatch an arbitrary exception.

        `HTTPException` -> `handle_http_exception`. Otherwise walks
        registered class handlers (MRO); on no match, logs via
        `log_exception` and returns 500. Pass `request=` to propagate
        the real failing request to the registered handler; omit to
        get a synthetic `GET /` for out-of-band callers (background
        tasks, CLI hooks).
        """
        if isinstance(exc, HTTPException):
            return await self.handle_http_exception(exc, request=request)
        handler = self._find_scoped_exception_handler(type(exc), request)
        if handler is not None:
            return await self._run_exc_handler(handler, exc, request)
        self.log_exception(exc, request)
        return JSONResponse(
            {"detail": MSG_INTERNAL_SERVER_ERROR},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    def _should_propagate_exceptions(self) -> bool:
        """Whether unhandled exceptions should re-raise out of dispatch.

        True when `app.config["PROPAGATE_EXCEPTIONS"]` is explicitly set,
        or implicitly when both DEBUG and TESTING are enabled.
        """
        explicit = self.config.get("PROPAGATE_EXCEPTIONS")
        if explicit is not None:
            # Coerced, not truth-tested: env-file loaders store strings, and
            # `bool("false")` is True.
            return _coerce_bool(explicit)
        return self.debug and _coerce_bool(self.config.get("TESTING"))

    def _find_exception_handler(self, exc_type: type[Any]) -> Callable[..., Any] | None:
        """Walk `exc_type`'s MRO looking for a registered handler.

        Handlers registered against a base class catch every subclass, so
        `@app.exception_handler(HTTPException)` catches every `NotFound`,
        `Forbidden`, etc. The result is cached per exception type.
        """
        cached = self._exc_handler_cache.get(exc_type, _MISSING)
        if cached is not _MISSING:
            return cached
        for cls in exc_type.__mro__:
            handler = self._exception_handlers.get(cls)
            if handler is not None:
                self._exc_handler_cache[exc_type] = handler
                return handler
        self._exc_handler_cache[exc_type] = None
        return None

    def _find_scoped_exception_handler(
        self, exc_type: type[Any], request: Request | None
    ) -> Callable[..., Any] | None:
        """Find an exception handler, preferring the request's blueprint chain.

        A blueprint's `@bp.errorhandler` only applies to exceptions raised on its
        own routes (or a nested descendant's), so the failing request's blueprint
        chain is walked innermost-first before falling back to the app-level
        MRO-cached lookup. Not cached - the error path is cold.
        """
        if request is not None and self._bp_exception_handlers:
            for bp_name in request.blueprints:
                table = self._bp_exception_handlers.get(bp_name)
                if not table:
                    continue
                for cls in exc_type.__mro__:
                    handler = table.get(cls)
                    if handler is not None:
                        return handler
        return self._find_exception_handler(exc_type)

    def _find_scoped_status_handler(
        self, code: int, request: Request | None
    ) -> Callable[..., Any] | None:
        """Find a status-code handler, preferring the request's blueprint chain."""
        if request is not None and self._bp_status_handlers:
            for bp_name in request.blueprints:
                table = self._bp_status_handlers.get(bp_name)
                if table:
                    handler = table.get(code)
                    if handler is not None:
                        return handler
        return self._status_handlers.get(code)

    async def _run_exc_handler(
        self, handler: Callable[..., Any], exc: BaseException, request: Request | None
    ) -> Response:
        """Invoke a matched error handler and coerce whatever it returns.

        Both callers are out-of-band entry points, so a caller may have no
        request to hand: the synthetic `GET /` exists so a handler that reads
        `request` gets an object rather than `None`.
        """
        if request is None:
            request = Request(
                method=HTTP_METHOD_GET, path="/", query_string="", headers={}, body=b""
            )
        result = await self._call_exc_handler(handler, request, exc)
        if isinstance(result, Response):
            return result
        return self._coerce_response(result)
