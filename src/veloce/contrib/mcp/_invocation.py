"""InvocationMixin — handler dispatch and instrumentation for `MCPServer`.

Runs a tool / resource / prompt handler through the shared `DependencyResolver`
under the same request-context binding the HTTP path uses, replaying the full
route lifecycle (middleware, before/after hooks, response shaping, teardowns) for
a route-backed tool and the bare DI graph for a pure `@app.mcp_tool`. Mixed into
`MCPServer`, so `self` resolves the dispatch core and `TasksMixin` at runtime; the
`TYPE_CHECKING` block declares the cross-member surface for mypy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from veloce._internal import _current_app_var, _current_request_var, _is_async_callable, offload
from veloce.contrib.mcp._helpers import (
    _inflight_var,
    _log_level_var,
    _notifier_var,
    _requester_var,
    _route_path_params,
    _RouteResponse,
    _ShortCircuit,
)
from veloce.contrib.mcp.context import MCPContext, _session_var
from veloce.contrib.mcp.errors import MCPError, _InvalidArgumentsError
from veloce.contrib.mcp.plan_bridge import _build_request, bind_arguments
from veloce.dependency import DependencyResolver
from veloce.exceptions import RequestValidationError
from veloce.helpers import g
from veloce.http.response import Response
from veloce.instrumentation import RequestMetrics
from veloce.routing.router import RouteMatch

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.registry import MCPTool

_logger = logging.getLogger(__name__)


def _argument_error_text(errors: Any) -> str:
    """Render binding errors as text a language model can act on.

    The wire form used to be `str()` of the error list - a Python repr, with
    single quotes and a `loc` path that reads as framework internals. A model
    receiving that has to guess which argument it got wrong; naming the argument
    and the expectation is the difference between a retry that can succeed and
    one that cannot.
    """
    if not isinstance(errors, list) or not errors:
        return "Invalid arguments"
    lines = []
    for err in errors:
        if not isinstance(err, dict):
            lines.append(str(err))
            continue
        loc = err.get("loc") or ()
        # Drop the source segment (`body` / `query` / `path`): over MCP every
        # value arrives as a tool argument, so only the argument name is
        # meaningful to the caller.
        parts = [str(p) for p in loc if p not in ("body", "query", "path", "header", "cookie")]
        name = ".".join(parts) if parts else "arguments"
        lines.append(f"{name}: {err.get('msg', 'invalid value')}")
    joined = "; ".join(lines)
    return f"Invalid arguments - {joined}"


class InvocationMixin:
    """Handler dispatch, lifecycle replay, and instrumentation for `MCPServer`."""

    # `MCPServer` is slotted; a mixin must declare `__slots__` too or its
    # instances regain a `__dict__` (see the __slots__ discipline rule).
    __slots__ = ()

    if TYPE_CHECKING:  # pragma: no cover
        # Attributes / methods the host server (and `TasksMixin`) provide.
        app: Any
        _call_timeout: Any

    # ── Invocation ────────────────────────────────────────

    async def _run_invoke(
        self, tool: MCPTool, arguments: dict[str, Any], progress_token: str | int | None
    ) -> Any:
        """Invoke a tool, applying the optional per-call timeout.

        With no `MCP_CALL_TIMEOUT` configured the handler runs unbounded (the
        common case, zero overhead); otherwise it is cancelled past the budget and
        the `asyncio.TimeoutError` is surfaced by the caller (in-band for a tool
        call, a JSON-RPC error for a resource read or prompt render).

        The call's binding of `request` / `g` / `current_app` is undone once it
        ends. The Streamable HTTP transport awaits `handle_message` from inside a
        live HTTP request, so a binding that outlived the call would leave the rest
        of that handler, and every hook after it, reading the call's synthetic
        request instead of the real one.
        """
        # A derived tool publishes a different argument surface from the one its
        # handler takes; translate before anything reads the arguments, so hooks,
        # binding and the handler all see one consistent mapping.
        if tool.derived_from is not None:
            arguments = tool.derived_from.translate(arguments)

        # Run before building the call: a hook that answers instead of the handler
        # must not leave an un-awaited coroutine behind.
        before = self.app._mcp_before_call
        if before:
            short_circuit = await self._run_before_call(tool.name, arguments, before)
            if short_circuit is not None:
                return short_circuit

        # Built here so the restore wraps one await, not a duplicated pair.
        call = (
            self._invoke(tool, arguments, progress_token)
            if self._call_timeout is None
            else asyncio.wait_for(self._invoke(tool, arguments, progress_token), self._call_timeout)
        )
        outer_app = _current_app_var.get()
        outer_request = _current_request_var.get()
        outer_globals = g._snapshot()
        unbind = True
        try:
            result = await call
            after = self.app._mcp_after_call
            if after:
                result = await self._run_after_call(tool.name, result, after)
            return result
        except GeneratorExit:
            # Closed mid-await: an abandoned call being finalized, which the
            # collector may run in any context. There is no awaiter to hand a
            # context back to, and restoring here would write the outer binding
            # into whichever context happens to be current.
            unbind = False
            raise
        finally:
            if unbind:
                _current_app_var.set(outer_app)
                _current_request_var.set(outer_request)
                g._restore(outer_globals)

    @staticmethod
    async def _run_before_call(
        name: str, arguments: dict[str, Any], hooks: list[Any]
    ) -> Any | None:
        """Run the pre-call hooks, returning the first short-circuit value."""
        for hook in hooks:
            outcome = hook(name, arguments)
            if _is_async_callable(hook):
                outcome = await outcome
            if outcome is not None:
                return outcome
        return None

    @staticmethod
    async def _run_after_call(name: str, result: Any, hooks: list[Any]) -> Any:
        """Run the post-call hooks in order, each seeing what the last returned."""
        for hook in hooks:
            outcome = hook(name, result)
            if _is_async_callable(hook):
                outcome = await outcome
            result = outcome
        return result

    async def _invoke(
        self, tool: MCPTool, arguments: dict[str, Any], progress_token: str | int | None = None
    ) -> Any:
        """Resolve DI and call the handler, draining teardowns afterwards.

        The handler runs inside the same request-context binding the HTTP path
        uses: `current_app` and `request` are bound onto their contextvars and
        `g` is reset, so a handler or dependency that reads `current_app` / `g`
        works.

        For a route-derived tool the full request lifecycle is replayed so the
        tool result matches the HTTP response: the matched path parameters are
        copied onto `request.path_params`, the app's `before_request` chain runs
        first (a hook returning a `Response` short-circuits the call), the
        handler return is shaped through the route's `_build_response`, the
        `after_request` chain runs and may rewrite that response, and the
        `teardown_request` / `teardown_appcontext` hooks fire in the `finally`.
        A handler exception is routed through the app's exception handlers (the
        same lookup the HTTP path uses) and the resulting response becomes the
        tool result. A pure `@app.mcp_tool` (no route) has no such lifecycle and
        its return value is passed back unchanged.
        """
        # The client capabilities gating `ctx.sample` / `elicit` / `roots` come
        # from the dispatching connection's session (recorded at `initialize`);
        # empty off a stateful transport, which leaves those methods to reject.
        session = _session_var.get()
        # The session and the server are passed as references, not unpacked: the
        # context exposes what they hold through properties, so a call that never
        # asks for them pays only these two assignments.
        context = MCPContext(
            tool.name,
            arguments,
            notifier=_notifier_var.get(),
            progress_token=progress_token,
            log_level=_log_level_var.get(),
            requester=_requester_var.get(),
            session=session,
            server=self,
        )
        # Expose this context on its in-flight registration so a
        # `notifications/cancelled` flips `ctx.cancelled` (cooperative stop) as
        # well as cancelling the task. `None` off-dispatch or for an untracked call.
        inflight = _inflight_var.get()
        if inflight is not None:
            inflight.context = context
        resolver = DependencyResolver()
        resolver._overrides = self.app._dependency_overrides
        resolver._override_subplans = self.app._override_subplans

        route_info = tool.route_info
        # Seed the synthetic request's value sources with the call arguments so
        # a sub-dependency `Query` / `Body` / `Header` / `Cookie` / `Form`
        # marker resolves from them, the same way a top-level tool parameter
        # does (see `_build_request`). A route-backed tool also adopts the
        # wrapped route's real HTTP method and rule path so anything branching
        # on `request.method` / `request.path` matches the HTTP path.
        if route_info is not None:
            request = _build_request(
                tool.name,
                arguments,
                method=tool.route_method,
                path=route_info.path_template or None,
            )
        else:
            request = _build_request(tool.name, arguments)
        request.app = self.app
        # Mark the synthetic request so auth middleware can recognise a replayed
        # MCP call (`request.is_mcp`) and defer to the transport's authentication
        # rather than re-checking a browser credential the agent never sends.
        request._state["_mcp"] = True

        # Bind the request context exactly as `handle_request` does: the
        # `current_app` / `request` contextvars plus a fresh `g`. `_run_invoke`
        # unbinds them once the call ends, so a caller that awaited this from
        # inside its own request keeps reading its own.
        _current_app_var.set(self.app)
        _current_request_var.set(request)
        g._reset()

        exc: BaseException | None = None
        bp_name: str | None = None

        # For a pure `@app.mcp_tool` there is no route lifecycle to replay - run
        # the handler with its DI graph and return the raw value, draining only
        # the yield-dependency teardowns. The exception path stays in-band.
        if route_info is None:
            try:
                return await self._invoke_pure(tool, arguments, context, resolver, request)
            except BaseException as err:  # noqa: BLE001 - re-raised after teardown
                exc = err
                raise
            finally:
                await resolver.run_teardowns(exc)

        # Route-derived tool: replay the matched-route state the HTTP path sets
        # before dispatch. `request.endpoint` and `url_rule` let a hook gate on
        # the route name and the blueprint bucket resolve; `path_params` carries
        # the tool arguments that name a route path parameter so a hook /
        # dependency / handler reading `request.path_params` sees them, exactly
        # as on the HTTP path.
        request.endpoint = route_info.name
        request._state["url_rule"] = route_info.path_template
        request.path_params = _route_path_params(route_info, arguments)

        try:
            # Request-phase middleware runs first, exactly as `_dispatch_request`
            # runs it before `before_request`, so a route depending on
            # middleware-populated state (a session loaded by `SessionMiddleware`,
            # a header set by a custom middleware) sees it over MCP too. A
            # middleware that short-circuits by returning a `Response` is treated
            # like a `before_request` short-circuit: shaped into the tool result
            # and run through the same teardown `finally`. Returned from *inside*
            # this `try` so DI + `teardown_request` / `teardown_appcontext` still
            # fire. The response middleware phase is intentionally not replayed:
            # the tool result is derived from the response body, not a wire
            # response, so a response-mutating middleware (compression, headers)
            # has nothing to act on.
            # A route declaring `exclude_middleware` must skip the excluded
            # middleware here too, so the MCP path matches HTTP dispatch (which
            # runs the route's filtered chain). `_route_middleware_chains`
            # returns `None` when the route excludes nothing - the common,
            # zero-cost case - in which case the full app chain runs.
            filtered = (
                self.app._route_middleware_chains(route_info)
                if route_info.excluded_middleware is not None
                else None
            )
            request_chain = filtered[0] if filtered is not None else None
            early = await self.app._run_request_middleware(request, request_chain)
            if early is not None:
                return _ShortCircuit(early)

            # `before_request` (app-level then matched blueprint). A
            # short-circuit response is the tool result; `bp_name` is recorded
            # so the matched blueprint's `after_request` / teardown hooks fire
            # even on short-circuit. The short-circuit returns from *inside* this
            # `try` so the `finally` still drains DI teardowns and runs
            # `teardown_request` / `teardown_appcontext` - the HTTP dispatch runs
            # its teardown even when `before_request` returns early, and a tool
            # that relies on `teardown_request` cleanup on a rejected call (an
            # auth 401 short-circuit) must get the same.
            early, bp_name = await self.app._run_before_hooks(request)
            if early is not None:
                return _ShortCircuit(early)

            # URL value preprocessors run after `before_request` and after
            # `path_params` is populated, exactly as the HTTP path runs them in
            # `_resolve_route`, so a processor that rewrites a path param or
            # seeds `g` (locale / tenant extraction) is observed by the
            # dependencies and the handler.
            self.app._run_url_value_preprocessors(route_info.name, request.path_params)

            result = await self._bind_and_call(tool, arguments, context, resolver, request)

            # `_build_response` runs the route `response_model` filter only over a
            # non-`Response` handler return; a handler that returned its own
            # `Response` keeps that body unfiltered. Record which case this is so
            # the server only advertises a filtered body as schema-conformant
            # `structuredContent`.
            model_filtered = not isinstance(result, Response)

            # Shape the handler return into the final `Response` exactly as the
            # HTTP path does (`_build_response` runs the route `response_model`
            # filtering + coercion + injected-response merge), then run the
            # `after_request` chain so a hook can rewrite the response before the
            # tool result is derived from it - mirroring HTTP dispatch order.
            match = RouteMatch(route_info, request.path_params)
            response = self.app._build_response(request, match, result)
            response = await self.app._run_after_hooks(request, response, bp_name)

            # Background work: the handler's injected queue plus any task it
            # attached to its own `Response`. Awaited inline (the stdio path has
            # no response to flush first); a task error is logged, never allowed
            # to fail the produced tool result.
            tasks = request._background_tasks
            if tasks is not None:
                try:
                    await tasks.run_all()
                except Exception:
                    _logger.exception("MCP background task failed")
            await self._run_response_background(response)
            return _RouteResponse(response, model_filtered)
        except MCPError:
            # Not a handled application failure: an error the author raised to
            # say something about the protocol, so it goes to the caller as the
            # code and message they wrote rather than through the app's exception
            # handlers, which would render it as an HTTP body. A malformed
            # argument (the `_InBandError` subtree) is shaped in-band by
            # `_tools_call` instead, so the model can retry. Both still flow
            # through the `finally` so teardowns run.
            raise
        except BaseException as err:  # noqa: BLE001 - re-raised / routed after teardown
            exc = err
            # Route the handler exception through the app's exception handlers
            # (the same status-code + class lookup the HTTP path uses) so a
            # route relying on `@app.exception_handler(...)` - or the default
            # `HTTPException` JSON body - yields the right MCP payload. A
            # `BaseException` that is not an `Exception` (e.g. cancellation)
            # has no handler path and is re-raised after teardown.
            if isinstance(err, Exception):
                response = await self.app.handle_user_exception(err, request=request)
                return _RouteResponse(response)
            raise
        finally:
            # Yield-dependency teardowns first (the resource was acquired before
            # the handler ran and must be released regardless of outcome), then
            # the `teardown_request` / `teardown_appcontext` hooks - both receive
            # the exception (or None), mirroring the HTTP dispatch `finally`.
            await resolver.run_teardowns(exc)
            await self.app._run_request_teardown(exc, bp_name)

    async def _invoke_pure(
        self,
        tool: MCPTool,
        arguments: dict[str, Any],
        context: MCPContext,
        resolver: DependencyResolver,
        request: Any,
    ) -> Any:
        """Run a pure `@app.mcp_tool` handler and return its raw value.

        No route lifecycle applies, so the return value is passed back unchanged
        (the caller stringifies it) and a handler exception propagates to be
        surfaced in-band by `_tools_call`.
        """
        result = await self._bind_and_call(tool, arguments, context, resolver, request)
        tasks = request._background_tasks
        if tasks is not None:
            try:
                await tasks.run_all()
            except Exception:
                _logger.exception("MCP background task failed")
        await self._run_response_background(result)
        return result

    async def _bind_and_call(
        self,
        tool: MCPTool,
        arguments: dict[str, Any],
        context: MCPContext,
        resolver: DependencyResolver,
        request: Any,
    ) -> Any:
        """Bind the handler kwargs from `arguments` and call the handler.

        The argument-binding boundary maps a malformed argument to an in-band
        tool-execution error: a missing argument (TypeError), a failed coercion
        (RequestValidationError) or a failed model validation (ValueError). The
        spec classes input validation as a tool execution error rather than a
        protocol error, because it is the class of failure a model can act on -
        clients feed execution errors back to the model to self-correct, and
        protocol errors generally not at all. The handler call lives outside that
        guard so a genuine TypeError / ValueError raised in the handler body
        propagates unchanged - surfaced in-band for a pure tool, routed through
        the app's exception handlers for a route-backed one.
        """
        # Route rule `defaults=` fill handler kwargs the call did not supply,
        # matching HTTP precedence (explicit argument > route default > Python
        # default). A pure `@app.mcp_tool` has no route, hence no defaults.
        route_info = tool.route_info
        route_defaults = route_info.defaults if route_info is not None else None
        try:
            kwargs, _request = await bind_arguments(
                tool.plan,
                arguments,
                context,
                resolver,
                tool.route_dep_plans,
                request=request,
                route_defaults=route_defaults,
            )
        except RequestValidationError as err:
            raise _InvalidArgumentsError(_argument_error_text(err.errors)) from err
        except (TypeError, ValueError) as err:
            raise _InvalidArgumentsError(str(err)) from err

        handler = tool.handler
        if _is_async_callable(handler):
            return await handler(**kwargs)
        # A sync handler runs in the thread pool so it cannot block the event
        # loop - the same offload the HTTP path applies; `offload` preserves
        # request-scoped ContextVars.
        return await offload(handler, **kwargs)

    @staticmethod
    async def _run_response_background(result: Any) -> None:
        """Run a returned `Response`'s `background` task, mirroring the HTTP path.

        The HTTP path schedules `response.background` (a `BackgroundTask` or a
        `BackgroundTasks` collection) in addition to the DI-injected queue.
        A task error is logged, never allowed to fail the produced tool result.
        """
        if not isinstance(result, Response):
            return
        background = result.background
        if background is None:
            return
        try:
            if hasattr(background, "run_all"):
                await background.run_all()
            elif hasattr(background, "run"):
                await background.run()
        except Exception:
            _logger.exception("MCP response background task failed")

    async def _instrument(self, tool: MCPTool, started: float, status_code: int) -> None:
        """Fire the app instrumentation hooks for a finished tool call.

        Reuses the same `RequestMetrics`/`add_instrumentation` contract the
        HTTP path uses: `method` is the JSON-RPC method, `route` is the tool
        name (a low-cardinality label), `path` the tool name too. `status_code`
        is the call's real outcome - the shaped `Response`'s status for a
        route-backed / short-circuited call, 500 for an unhandled handler error
        or a stream that overran the buffer limit, 200 only on genuine success -
        so a 4xx/5xx is never misreported as 200.
        """
        hooks = self.app._instrumentation
        if not hooks:
            return
        duration_ms = (time.perf_counter() - started) * 1000.0
        metrics = RequestMetrics(
            method="tools/call",
            path=tool.name,
            route=tool.name,
            status_code=status_code,
            duration_ms=duration_ms,
        )
        for hook in hooks:
            try:
                outcome = hook(metrics)
                if asyncio.iscoroutine(outcome):
                    await outcome
            except Exception:
                _logger.exception("instrumentation hook raised an exception")
