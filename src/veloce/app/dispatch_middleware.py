"""Middleware phases and instrumentation for the dispatch pipeline."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import veloce.status as status
from veloce.app._host import AppHost
from veloce.app.dispatch_support import _MW_RESPONSE_CHAIN_KEY, _trace_carrier
from veloce.http.request import Request
from veloce.http.response import (
    Response,
)
from veloce.instrumentation import RequestMetrics
from veloce.middleware import Middleware


class MiddlewareDispatchMixin(AppHost):
    """Request/response middleware phases and request instrumentation."""

    async def _run_request_middleware(
        self, request: Request, chain: list[Middleware] | None = None
    ) -> Response | None:
        """Run the middleware request phase in registration order.

        The first `process_request` to return a `Response` short-circuits the
        chain. `chain` defaults to the app's full middleware list; the MCP caller
        passes the route's pre-filtered chain so `exclude_middleware` applies there.
        """
        for mw in self._middlewares if chain is None else chain:
            early_response = await mw.process_request(request)
            if early_response is not None:
                return early_response
        return None

    def _route_middleware_chains(
        self, route_info: Any
    ) -> tuple[list[Middleware], list[Middleware]] | None:
        """Resolve the filtered (request-order, response-order) chains for a route.

        `None` when the route excludes nothing, signalling callers to use the
        app's middleware list directly with no copy or filter. Otherwise the
        chains are memoised on the RouteInfo, keyed on `self._mw_version`.
        """
        excluded = route_info.excluded_middleware
        if excluded is None:
            return None
        cache = route_info._mw_chain_cache
        version = self._mw_version
        if cache is not None and cache[0] == version:
            return cache[1], cache[2]
        names, types = excluded
        request_chain = [
            mw
            for mw in self._middlewares
            if mw.middleware_name not in names and not isinstance(mw, types)
        ]
        response_chain = request_chain[::-1]
        route_info._mw_chain_cache = (version, request_chain, response_chain)
        return request_chain, response_chain

    async def _run_response_phase(
        self,
        fused: tuple[Callable[..., Any], ...] | None,
        request: Request,
        response: Response,
        excluded: bool,
    ) -> Response:
        """Apply the response phase, preferring the compile-time fused chain.

        `excluded` records whether this request stashed a per-route filtered
        response chain, passed in rather than re-probed from `request._state`
        per response. `fused` is `None` only when no middleware is registered.
        """
        if excluded:
            return await self._run_response_middleware(request, response)
        if fused is not None:
            for process_response in fused:
                response = await process_response(request, response)
        return response

    async def _run_response_middleware(self, request: Request, response: Response) -> Response:
        """Run middleware response phase in reverse order.

        Honours a per-route filtered chain stashed on `request._state` by the
        request phase, so `exclude_middleware` applies symmetrically.
        """
        chain = request._state.get(_MW_RESPONSE_CHAIN_KEY)
        if chain is None:
            chain = reversed(self._middlewares)
        for mw in chain:
            response = await mw.process_response(request, response)
        return response

    async def _run_instrumentation(
        self,
        request: Request,
        status_code: int,
        duration_ms: float,
        streamed: bool = False,
        end_time_ns: int | None = None,
    ) -> None:
        """Deliver a `RequestMetrics` record to every instrumentation hook.

        A hook may be sync or async; one that raises is logged and skipped so
        observability code can never break the response. `streamed` marks a
        response whose `duration_ms` / `status_code` cover only its production,
        not stream completion.
        """
        # Only for a 5xx, so a deliberate 5xx return is not mislabelled.
        error_type = (
            request._state.get("_error_type")
            if status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR
            else None
        )
        metrics = RequestMetrics(
            method=request.method,
            path=request.path,
            route=request.url_rule,
            status_code=status_code,
            duration_ms=duration_ms,
            streamed=streamed,
            end_time_ns=end_time_ns,
            error_type=error_type,
            # Built on every dispatch path - a before_request hook could be skipped.
            parent_context=_trace_carrier(request),
        )
        # Sparse, so the membership test is skipped when none are configured.
        excludes = self._instrumentation_excludes
        route = metrics.route
        for hook in self._instrumentation:
            if excludes and route is not None:
                excluded = excludes.get(hook)
                if excluded is not None and route in excluded:
                    continue
            try:
                result = hook(metrics)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                self.logger.exception("instrumentation hook raised an exception")
