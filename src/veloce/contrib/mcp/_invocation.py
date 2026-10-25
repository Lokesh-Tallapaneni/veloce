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
from typing import TYPE_CHECKING, Any, cast

from veloce._internal import _current_app_var, _current_request_var, _is_async_callable, offload
from veloce.contrib.mcp._helpers import (
    _inflight_var,
    _log_level_var,
    _notifier_var,
    _request_id_var,
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
    from veloce.contrib.mcp.server import MCPServer

_logger = logging.getLogger(__name__)


def _argument_error_text(errors: Any) -> str:
    """Render binding errors as text a language model can act on.

    Not `str()` of the error list: a model receiving that repr has to guess
    which argument it got wrong, so the name and the expectation are spelled out.
    """
    if not isinstance(errors, list) or not errors:
        return "Invalid arguments"
    lines = []
    for err in errors:
        if not isinstance(err, dict):
            lines.append(str(err))
            continue
        loc = err.get("loc") or ()
        # Over MCP every value is a tool argument, so the source segment is not.
        parts = [str(p) for p in loc if p not in ("body", "query", "path", "header", "cookie")]
        name = ".".join(parts) if parts else "arguments"
        lines.append(f"{name}: {err.get('msg', 'invalid value')}")
    joined = "; ".join(lines)
    return f"Invalid arguments - {joined}"


def _resolve_log_level(session: Any) -> str | None:
    """Resolve the level this call's notifications are filtered against.

    Per-request first, then per-connection: a modern request names its level in
    `_meta`, while `logging/setLevel` records the connection's choice on the
    session, which is what carries between requests.
    """
    level = _log_level_var.get()
    if level is not None:
        return level
    return getattr(session, "log_level", None)


class InvocationMixin:
    """Handler dispatch, lifecycle replay, and instrumentation for `MCPServer`."""

    # `MCPServer` is slotted, so a mixin without this regains a `__dict__`.
    __slots__ = ()

    if TYPE_CHECKING:  # pragma: no cover
        # Attributes / methods the host server (and `TasksMixin`) provide.
        app: Any
        _call_timeout: Any

    # ── Invocation ────────────────────────────────────────

    async def _run_invoke(
        self,
        tool: MCPTool,
        arguments: dict[str, Any],
        progress_token: str | int | None,
        request_meta: dict[str, Any] | None = None,
    ) -> Any:
        """Invoke a tool, applying the optional per-call timeout.

        With no `MCP_CALL_TIMEOUT` configured the handler runs unbounded; otherwise
        it is cancelled past the budget and the `asyncio.TimeoutError` is surfaced
        by the caller.

        The call's binding of `request` / `g` / `current_app` is undone once it
        ends: the Streamable HTTP transport awaits `handle_message` from inside a
        live HTTP request, which must go on reading its own request.
        """
        # A derived tool publishes a different argument surface; translate first.
        if tool.derived_from is not None:
            arguments = tool.derived_from.translate(arguments)

        # Before the call is built: a hook that answers it must not leave a
        # coroutine un-awaited.
        before = self.app._mcp_before_call
        if before:
            short_circuit = await self._run_before_call(tool.name, arguments, before)
            if short_circuit is not None:
                return short_circuit

        call = (
            self._invoke(tool, arguments, progress_token, request_meta)
            if self._call_timeout is None
            else asyncio.wait_for(
                self._invoke(tool, arguments, progress_token, request_meta), self._call_timeout
            )
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
            # An abandoned call has no awaiter to hand the outer binding back to.
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
        self,
        tool: MCPTool,
        arguments: dict[str, Any],
        progress_token: str | int | None = None,
        request_meta: dict[str, Any] | None = None,
    ) -> Any:
        """Resolve DI and call the handler, draining teardowns afterwards.

        The handler runs inside the same request-context binding the HTTP path
        uses, so a handler or dependency reading `current_app` / `g` works.

        For a route-derived tool the full request lifecycle is replayed - path
        params, `before_request`, `_build_response`, `after_request`, teardowns,
        and the app's exception handlers - so the tool result matches the HTTP
        response. A pure `@app.mcp_tool` has no such lifecycle and its return
        value is passed back unchanged.
        """
        # The capabilities gating `ctx.sample` are recorded at `initialize`.
        session = _session_var.get()
        context = MCPContext(
            tool.name,
            arguments,
            notifier=_notifier_var.get(),
            progress_token=progress_token,
            log_level=_resolve_log_level(session),
            requester=_requester_var.get(),
            session=session,
            # Only the subclass declares that `self` is the `MCPServer`.
            server=cast("MCPServer", self),
            request_meta=request_meta,
            request_id=_request_id_var.get(),
        )
        # So a `notifications/cancelled` also flips `ctx.cancelled`.
        inflight = _inflight_var.get()
        if inflight is not None:
            inflight.context = context
        resolver = DependencyResolver()
        resolver._overrides = self.app._dependency_overrides
        resolver._override_subplans = self.app._override_subplans

        route_info = tool.route_info
        # Seeded with the call arguments and the route's real method and path.
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
        # So auth middleware reads `request.is_mcp` and defers to the transport.
        request._state["_mcp"] = True

        # Bound as `handle_request` does; `_run_invoke` unbinds them afterwards.
        _current_app_var.set(self.app)
        _current_request_var.set(request)
        g._reset()

        exc: BaseException | None = None
        bp_name: str | None = None

        if route_info is None:
            try:
                return await self._invoke_pure(tool, arguments, context, resolver, request)
            except BaseException as err:  # noqa: BLE001 - re-raised after teardown
                exc = err
                raise
            finally:
                await resolver.run_teardowns(exc)

        # The matched-route state the HTTP path sets before dispatch.
        request.endpoint = route_info.name
        request._state["url_rule"] = route_info.path_template
        request.path_params = _route_path_params(route_info, arguments)

        try:
            # Request-phase middleware first, as `_dispatch_request` runs it. The
            # response phase is not replayed: the result comes from the body.
            filtered = (
                self.app._route_middleware_chains(route_info)
                if route_info.excluded_middleware is not None
                else None
            )
            request_chain = filtered[0] if filtered is not None else None
            early = await self.app._run_request_middleware(request, request_chain)
            if early is not None:
                return _ShortCircuit(early)

            # Returned inside this `try`, so a short-circuit still tears down.
            early, bp_name = await self.app._run_before_hooks(request)
            if early is not None:
                return _ShortCircuit(early)

            # After `path_params`, as `_resolve_route` runs them.
            self.app._run_url_value_preprocessors(route_info.name, request.path_params)

            result = await self._bind_and_call(tool, arguments, context, resolver, request)

            # `_build_response` filters only a non-`Response` return.
            model_filtered = not isinstance(result, Response)

            match = RouteMatch(route_info, request.path_params)
            response = self.app._build_response(request, match, result)
            response = await self.app._run_after_hooks(request, response, bp_name)

            await self._run_background(request, response)
            return _RouteResponse(response, model_filtered)
        except MCPError:
            # Raised about the protocol, so it is not rendered as an HTTP body.
            raise
        except BaseException as err:  # noqa: BLE001 - re-raised / routed after teardown
            exc = err
            # Through the app's exception handlers, as the HTTP path does.
            if isinstance(err, Exception):
                response = await self.app.handle_user_exception(err, request=request)
                return _RouteResponse(response)
            raise
        finally:
            # In the order of the HTTP dispatch `finally`, with the exception.
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
        await self._run_background(request, result)
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

        The spec classes input validation as a tool execution error rather than a
        protocol error, because clients feed execution errors back to the model to
        self-correct, so the binding boundary maps a missing argument, a failed
        coercion or a failed model validation to one. The handler call lives
        outside that guard, so a `TypeError` from the handler body propagates.
        """
        # HTTP precedence: explicit argument > route default > Python default.
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
                dependency_inputs=tool.dependency_inputs,
            )
        except RequestValidationError as err:
            raise _InvalidArgumentsError(_argument_error_text(err.errors)) from err
        except (TypeError, ValueError) as err:
            raise _InvalidArgumentsError(str(err)) from err

        handler = tool.handler
        if _is_async_callable(handler):
            return await handler(**kwargs)
        # The same offload the HTTP path applies, preserving request ContextVars.
        return await offload(handler, **kwargs)

    async def _run_background(self, request: Any, result: Any) -> None:
        """Run everything this call scheduled: the injected queue, then the response's.

        Awaited inline, since the stdio path has no response to flush first; a
        task error is logged, never allowed to fail the produced tool result.
        """
        tasks = request._background_tasks
        if tasks is not None:
            try:
                await tasks.run_all()
            except Exception:
                _logger.exception("MCP background task failed")
        await self._run_response_background(result)

    @staticmethod
    async def _run_response_background(result: Any) -> None:
        """Run a returned `Response`'s `background` task, mirroring the HTTP path.

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
            else:
                # Assigned after construction, so never `coerce_background`-ed.
                _logger.warning(
                    "MCP response background %r has no run()/run_all() and was not run",
                    type(background).__name__,
                )
        except Exception:
            _logger.exception("MCP response background task failed")

    async def _instrument(self, tool: MCPTool, started: float, status_code: int) -> None:
        """Fire the app instrumentation hooks for a finished tool call.

        Reuses the same `RequestMetrics`/`add_instrumentation` contract the HTTP
        path uses: `method` is the JSON-RPC method and `route` and `path` are both
        the tool name, a low-cardinality label. `status_code` is the call's real
        outcome, so a 4xx/5xx is never misreported as 200.
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
