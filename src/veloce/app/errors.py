"""Exception handling — registration and dispatch mixed into Veloce.

Holds the error-handler registry (`exception_handler` / `register_error_handler`
/ `add_exception_handler`), the MRO-walked handler lookup, and the
`handle_http_exception` / `handle_user_exception` builders that the request
pipeline calls when a handler raises. A mixin on `Veloce`; the per-request hot
path never reaches here, only the error path does. Kept out of `app.core` so the
error surface is one file and the core <-> errors import direction stays one-way.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import veloce.status as status
from veloce._constants import (
    HEADER_ALLOW,
    MIME_TEXT_PLAIN,
    MSG_INTERNAL_SERVER_ERROR,
)
from veloce._internal import _coerce_bool
from veloce._protocol_constants import (
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
)
from veloce.exceptions import HTTPException, _error_handler_key_error, http_exception_payload
from veloce.http.request import Request
from veloce.http.response import (
    JSONResponse,
    Response,
)

# Sentinel for cache misses where `None` is itself a valid cache hit ("no
# exception handler matched this type"). Plain `cache.get(k)` would re-walk the
# MRO every time for an unhandled exception type.
_MISSING: Any = object()


class ErrorsMixin:
    """Exception-handler registration and dispatch, mixed into `Veloce`."""

    if TYPE_CHECKING:  # pragma: no cover
        # Attributes / methods the host application (Veloce) provides.
        config: Any
        debug: bool
        logger: Any
        _assert_mutable: Callable[..., Any]
        _status_handlers: Any
        _exception_handlers: Any
        _exc_handler_cache: Any
        _bp_exception_handlers: Any
        _bp_status_handlers: Any
        _call_exc_handler: Callable[..., Any]
        _coerce_response: Callable[..., Any]
        get_allowed_methods: Callable[..., Any]

    def register_error_handler(self, code_or_exception: int | type, func: Callable) -> None:
        """Register an error handler without a decorator.

        The key is an `int` status code or an exception class. Anything else is
        refused: a non-class key landed in `_exception_handlers`, which is matched
        by walking a raised exception's MRO, so it could never be found.
        `exception_handlers={"404": h}` - realistic when the mapping is read from
        JSON, TOML or the environment - registered without a word and never fired.
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
            # The MRO-walk cache is invalidated on any registration so a
            # newly-added handler for a base class takes effect for the
            # already-cached subclasses.
            self._exc_handler_cache.clear()

    def exception_handler(self, exc_class_or_status: type | int) -> Callable:
        """Register a custom exception handler by exception type or status code."""

        def decorator(func: Callable) -> Callable:
            self.register_error_handler(exc_class_or_status, func)
            return func

        return decorator

    # A one-word spelling of the same decorator, for code that reads better
    # without the underscore. Semantics are identical - this is an alias, not a
    # second implementation.
    errorhandler = exception_handler

    def add_exception_handler(self, exc_class_or_status: type | int, handler: Callable) -> None:
        """Imperative exception-handler registration - ASGI shape.

        The non-decorator form of `@app.exception_handler(...)`.
        Accepts an exception class (matched by MRO at dispatch time) or
        an int HTTP status code.
        """
        self.register_error_handler(exc_class_or_status, handler)

    def log_exception(self, exc: BaseException, request: Request | None = None) -> None:
        """Log an exception with traceback.

        Routes the exception through the app logger at ERROR level. Used
        internally before falling back to a 500 response; exposed publicly so
        error-handler code can re-log via the same path.

        `request` names the request that failed, which is most of the value of
        the record: a traceback with no path is hard to place in a live log.
        Callers with no request in hand (a background task, a CLI hook) omit it.

        Silencing this is `logging.getLogger(app.import_name).setLevel(...)` or
        any other ordinary logging configuration - it is the app's own logger,
        deliberately, so an operator turns it down the way they turn down
        anything else.
        """
        if request is not None:
            self.logger.error("Exception on %s [%s]", request.path, request.method, exc_info=exc)
        else:
            self.logger.error("Exception on request", exc_info=exc)

    def make_default_options_response(
        self, path: str, allowed_methods: list[str] | None = None
    ) -> Response:
        """Build the auto-OPTIONS response for `path`.

        Returns a 200 response with an empty body and an `Allow` header
        listing every method registered for `path`, augmented with
        `HEAD` (whenever `GET` is supported) and `OPTIONS` itself per
        RFC 9110 Sec. 9.3.7. Callers that register an explicit OPTIONS
        handler can use this to compose the default `Allow` set. Pass
        `allowed_methods` when the registered set is already known to skip
        the redundant `get_allowed_methods` lookup.
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

        Walks registered status-code + class handlers first (matching
        `abort()` semantics), falling back to JSON
        `{"detail": exc.detail, "status_code": exc.status_code}` with
        `exc.headers` applied - byte-identical to what the request cycle
        emits for the same exception, so a handler reached over MCP or from
        a background task reports the error exactly as it does over HTTP.

        Pass `request=` when calling from inside a request scope so the
        registered error handler receives the real failing request
        (with the actual `path`, `method`, `path_params`, `state`, etc.)
        instead of a synthetic `GET /`. Callers without a request (the
        original out-of-band use case) can omit it.
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

    def _find_exception_handler(self, exc_type: type) -> Callable | None:
        """Walk `exc_type`'s MRO looking for a registered handler.

        Handlers registered against a base class catch every subclass -
        e.g. `@app.exception_handler(HTTPException)` catches every
        `NotFound`, `Forbidden`, etc. raised through `abort()`. The
        lookup result is cached per exception type; the cache is cleared
        on every `register_error_handler` call.
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
        self, exc_type: type, request: Request | None
    ) -> Callable | None:
        """Find an exception handler, preferring the request's blueprint chain.

        A blueprint's `@bp.errorhandler` only applies to exceptions raised on its
        own routes (or a nested descendant's): walk the failing request's
        blueprint chain innermost-first, returning the first per-blueprint handler
        whose registered exception class matches `exc_type`'s MRO. Falls back to
        the app-level (MRO-cached) lookup when no blueprint handler matches, so an
        app-level handler still catches everything and an app-level route is
        unaffected. Not cached - the error path is cold.
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

    def _find_scoped_status_handler(self, code: int, request: Request | None) -> Callable | None:
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
        self, handler: Callable, exc: BaseException, request: Request | None
    ) -> Response:
        """Invoke a matched error handler and coerce whatever it returns.

        Shared by `handle_http_exception` and `handle_user_exception`, which
        carried this block byte-for-byte twice. Both are out-of-band entry
        points, so a caller may have no request to hand: the synthetic `GET /`
        exists so a handler that reads `request` gets an object rather than
        `None`, and a real request is used whenever the caller has one.

        The error path, so the extra call costs nothing that matters.
        """
        if request is None:
            request = Request(
                method=HTTP_METHOD_GET, path="/", query_string="", headers={}, body=b""
            )
        result = await self._call_exc_handler(handler, request, exc)
        if isinstance(result, Response):
            return result
        return self._coerce_response(result)
