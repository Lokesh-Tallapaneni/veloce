"""Error shaping for the dispatch pipeline — request phase, exception handlers, 4xx/5xx bodies."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

import veloce.status as status
from veloce._constants import (
    MIME_TEXT_HTML_UTF8,
    MIME_TEXT_PLAIN_UTF8,
    MSG_INTERNAL_SERVER_ERROR,
)
from veloce._internal import (
    _serving_error_page_var,
    dumps_for,
)
from veloce._pipeline import (
    CompiledPipeline,
)
from veloce.app._host import AppHost
from veloce.app.dispatch_support import (
    _MW_RESPONSE_CHAIN_KEY,
    _adapt_hook_kwargs,
    _exc_handler_sig_cache,
    _prefers_html,
)
from veloce.debug import render_traceback_html
from veloce.exceptions import (
    HTTPException,
    http_exception_payload,
)
from veloce.http.request import Request
from veloce.http.response import (
    JSONResponse,
    Response,
    header_present,
)


class ErrorDispatchMixin(AppHost):
    """Exception handling and error-response shaping for dispatch."""

    async def _run_request_phase(
        self, request: Request, match: Any, cp: CompiledPipeline
    ) -> tuple[Response | None, bool]:
        """Run the request-phase middleware; return `(early_response, excluded)`.

        A route declaring `exclude_middleware` runs a memoised filtered chain and
        stashes the matching response-phase chain on `request._state` for
        symmetric skip. A non-`None` response has been through the response phase.
        """
        if match is not None and match.route_info.excluded_middleware is not None:
            # `None` would mean no exclusions, which the caller ruled out.
            filtered = self._route_middleware_chains(match.route_info)
            request._state[_MW_RESPONSE_CHAIN_KEY] = filtered[1]  # type: ignore[index]  # filtered is a pair once the chain is set
            for mw in filtered[0]:  # type: ignore[index]  # filtered is a pair once the chain is set
                early = await mw.process_request(request)
                if early is not None:
                    return await self._run_response_middleware(request, early), True
            return None, True
        # `or ()` keeps the loop typed; the caller's guard set `cp.http_pre`.
        for process_request in cp.http_pre or ():
            early = await process_request(request)
            if early is not None:
                return await self._run_response_phase(cp.http_post, request, early, False), False
        return None, False

    async def _run_exc_handler_response(
        self,
        request: Request,
        exc: Exception,
        handler: Callable[..., Any],
        cp: CompiledPipeline,
        excluded: bool,
    ) -> Response:
        """Run a registered exception handler and apply the response phase."""
        response = await self._dispatch_exc_handler(handler, request, exc)
        response = await self._run_response_phase(cp.http_post, request, response, excluded)
        return response

    def _find_error_handler(
        self, request: Request, exc: HTTPException
    ) -> Callable[..., Any] | None:
        """Find the error handler for `exc` on this request, or `None`.

        A status-code handler wins over a class handler; the class walk follows
        the MRO, and both prefer the request's blueprint chain over the
        app-level tables. Returns the handler, not the response, because each
        caller owns a different default and a different phase to run.
        """
        return self._find_scoped_status_handler(
            exc.status_code, request
        ) or self._find_scoped_exception_handler(type(exc), request)

    async def _http_exception_response(
        self,
        request: Request,
        exc: HTTPException,
        cp: CompiledPipeline | None,
        excluded: bool = False,
    ) -> Response:
        """Build the response for an `HTTPException` and run the response phase.

        The default body is `{"detail": ..., "status_code": ...}` with
        `exc.headers` applied; a validation error emits its structured `.errors`
        list as `{"detail": [...]}`. `cp` is `None` for a caller with no pipeline.
        """
        handler = self._find_error_handler(request, exc)
        if handler is not None:
            response = await self._dispatch_exc_handler(handler, request, exc)
        else:
            response = JSONResponse.from_bytes(
                dumps_for(self, http_exception_payload(exc)),
                status_code=exc.status_code,
                headers=exc.headers or None,
            )
        # `excluded` implies middleware exists, so `http_post` is not None.
        if cp is not None and cp.http_post is not None:
            response = await self._run_response_phase(cp.http_post, request, response, excluded)
        return response

    async def _shape_server_error(
        self, request: Request, exc: Exception, cp: CompiledPipeline, excluded: bool
    ) -> Response:
        """Shape an unhandled, non-propagated exception into a 500 response.

        In debug mode an HTML client gets the rich traceback page and every
        other client the plain-text traceback.
        """
        if self.debug:
            if _prefers_html(request):
                body = render_traceback_html(exc).encode()
                content_type = MIME_TEXT_HTML_UTF8
            else:
                body = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ).encode()
                content_type = MIME_TEXT_PLAIN_UTF8
            response = Response(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                body=body,
                content_type=content_type,
            )
            response = await self._run_response_phase(cp.http_post, request, response, excluded)
            return response
        return await self._handle_error(
            request,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            JSONResponse(
                {"detail": MSG_INTERNAL_SERVER_ERROR},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            ),
            exc,
        )

    async def _handle_error(
        self, request: Request, status_code: int, default: Response, exc: BaseException
    ) -> Response:
        """Build an error whose body is composed here, through a handler when one matches.

        For the refusals that are not raised - the 405 and the degraded 500.
        `default` is what the framework answers when nothing is registered.
        """
        if isinstance(exc, HTTPException):
            # Status table then class table, so a broad handler reshapes this too.
            handler = self._find_error_handler(request, exc)
            response = None
            if handler is not None:
                response = await self._dispatch_exc_handler(handler, request, exc)
                # RFC 9110 Sec. 15.5.6 makes `Allow` a MUST on a 405, so a
                # handler answering that same status inherits it.
                if exc.headers and response.status_code == exc.status_code:
                    for _name, _value in exc.headers.items():
                        if not header_present(response.headers, _name):
                            response.headers[_name] = _value
        else:
            # A `@bp.errorhandler(500)` wins; the class tables are deliberately
            # not re-walked, since the caller already missed them.
            handler = self._find_scoped_status_handler(status_code, request)
            # Through the same guard: a handler raising here has no outer `except`.
            response = (
                await self._dispatch_exc_handler(handler, request, exc)
                if handler is not None
                else None
            )
        return await self._run_response_middleware(
            request, default if response is None else response
        )

    async def _call_exc_handler(
        self, handler: Callable[..., Any], request: Request, exc: BaseException
    ) -> Any:
        """Call an exception handler, adapting kwargs to match its signature.

        A file such a handler returns stands in for a representation the request
        did not get, so the error-page flag set here keeps it from being sliced
        to that request's `Range`.
        """
        kwargs = _adapt_hook_kwargs(handler, _exc_handler_sig_cache, "exc", request, exc)
        token = _serving_error_page_var.set(True)
        try:
            return await self._call_handler(handler, kwargs)
        finally:
            _serving_error_page_var.reset(token)

    async def _dispatch_exc_handler(
        self, handler: Callable[..., Any], request: Request, exc: BaseException
    ) -> Response:
        """Invoke a user exception handler with a guard around its own raises.

        The secondary failure is logged, naming the handler and the request
        path, and the standard 500 returned; under `PROPAGATE_EXCEPTIONS` it is
        re-raised so the handler bug is visible.
        """
        try:
            result = await self._call_exc_handler(handler, request, exc)
        except Exception as handler_exc:
            if self._should_propagate_exceptions():
                raise
            handler_exc.__context__ = exc
            self.logger.error(
                "Exception handler %s raised while handling %s %s",
                getattr(handler, "__qualname__", repr(handler)),
                request.method,
                request.path,
                exc_info=handler_exc,
            )
            return self._coerce_response(
                JSONResponse(
                    {
                        "detail": MSG_INTERNAL_SERVER_ERROR,
                        "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    },
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            )
        return self._coerce_response(result)
