"""The dependency resolver — walks a pre-built plan once per request."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from veloce._constants import (
    MSG_YIELD_NO_VALUE,
    STATE_INJECTED_RESPONSE,
)
from veloce._handler_plan import (
    K_BG_TASKS,
    K_BODY_MODEL,
    K_DEPENDS,
    K_MODEL_GROUP,
    K_PARAM_MARKER,
    K_QUERY,
    K_QUERY_LIST,
    K_REQUEST,
    K_RESPONSE,
    K_SECURITY_SCOPES,
    K_UPLOAD_FILE,
    K_WEBSOCKET,
    MARKER_LOC,
    build_plan,
    build_route_dep_plans,
)
from veloce._internal import (
    _BaseExceptionGroup,
    _is_async_callable,
    offload,
)
from veloce._resolver_codegen import compile_graph_resolver, compile_param_resolver
from veloce.background import BackgroundTasks
from veloce.exceptions import RequestValidationError

if TYPE_CHECKING:  # pragma: no cover
    pass
from veloce._dependency_body import _BodyResolutionMixin
from veloce._dependency_coerce import (
    _coerce_scalar,
    _coerce_value,
    _collect_sequence_marker,
    _err_missing_marker,
    _validate_body_list,
    _validate_list_marker,
)
from veloce._dependency_markers import Depends, SecurityScopes
from veloce._dependency_params import _every_value, _resolve_list_param, _resolve_scalar_param
from veloce.http.request import Request
from veloce.http.response import Response

# Logged under the package name: users filter on `veloce.dependency`.
_logger = logging.getLogger("veloce.dependency")

# A rejected compiled-resolver build, so `resolve_plan` does not retry it.
_NOT_COMPILABLE = object()

# Shared sentinels; `_exec_depends` swaps the slot rather than mutating one.
_EMPTY_OVERRIDES: dict[Callable[..., Any], Callable[..., Any]] = {}
_EMPTY_OVERRIDE_SUBPLANS: weakref.WeakKeyDictionary[Callable[..., Any], Any] = (
    weakref.WeakKeyDictionary()
)

# ── Resolver ──────────────────────────────────────────────


class DependencyResolver(_BodyResolutionMixin):
    """Walks a `HandlerPlan` to produce kwargs for a handler.

    Per-request lifecycle: `_cache` is cleared on entry to `resolve_plan`.
    `_overrides` is set by the application (dependency
    overrides) and persists across requests.
    """

    __slots__ = (
        "_cache",
        "_overrides",
        "_override_subplans",
        "_teardowns",
        "_scope_stack",
        "_mcp_context",
    )

    def __init__(self) -> None:
        self._cache: dict[Callable[..., Any], Any] = {}
        # Overwritten by `_dispatch_request`, so the sentinels save two allocations.
        self._overrides: dict[Callable[..., Any], Callable[..., Any]] = _EMPTY_OVERRIDES
        self._override_subplans: weakref.WeakKeyDictionary[Callable[..., Any], Any] = (
            _EMPTY_OVERRIDE_SUBPLANS
        )
        # `(kind, generator)` pairs, drained in reverse by `run_teardowns()`.
        self._teardowns: list[tuple[str, Any]] = []
        # Read by a `SecurityScopes` parameter inside the chain.
        self._scope_stack: list[str] = []
        # Bound by the slot's declared TYPE; `None` on HTTP / WebSocket.
        self._mcp_context: Any = None

    def reset(self) -> None:
        """Clear the per-request resolver state.

        Run at the top of every path that resolves against this instance. The
        dispatcher does not call it, allocating a resolver per request instead.
        """
        self._cache.clear()
        self._teardowns.clear()
        self._scope_stack.clear()

    async def resolve_plan(
        self,
        plan: Any,
        request: Request,
        path_params: dict[str, str],
        route_dep_plans: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Fast path - consume a pre-built `HandlerPlan`.

        Args:
            plan: The handler's pre-built plan.
            request: The request the parameters come from.
            path_params: Path parameters matched for the route.
            route_dep_plans: Plans of dependencies declared on the route itself, run
                before the handler's own.

        Returns:
            The keyword arguments to call the handler with.

        Raises:
            RequestValidationError: If a parameter is missing or fails validation.
            HTTPException: If a dependency or security scheme refuses the request.

        """
        # Public, so a reused instance must not leak a prior resolve's state.
        self.reset()

        # Compiled once and cached on the plan; `_NOT_COMPILABLE` interprets.
        if not route_dep_plans:
            cr = plan.compiled_resolver
            if cr is None:
                compiled = compile_param_resolver(plan, _coerce_value, RequestValidationError)
                plan.compiled_resolver = cr = compiled if compiled is not None else _NOT_COMPILABLE
            if cr is not _NOT_COMPILABLE:
                return cr(request, path_params)  # pyright: ignore[reportCallIssue]

            # A no-wave graph compiles too, but its body reads neither the override
            # map nor the MCP context, so it is used only when both are absent.
            if not self._overrides and self._mcp_context is None:
                gcr = plan.compiled_graph_resolver
                if gcr is None:
                    graph = compile_graph_resolver(
                        plan,
                        _coerce_value,
                        RequestValidationError,
                        offload,
                        BackgroundTasks,
                        Response,
                        SecurityScopes,
                    )
                    plan.compiled_graph_resolver = gcr = (
                        graph if graph is not None else _NOT_COMPILABLE
                    )
                if gcr is not _NOT_COMPILABLE:
                    resolver = cast("Callable[..., Any]", gcr)
                    return await resolver(request, path_params, self._teardowns)
        else:
            for slot in route_dep_plans:
                await self._exec_depends(slot, request, path_params)

        return await self._resolve_slots(plan, request, path_params)

    async def resolve_ws_plan(
        self,
        plan: Any,
        websocket: Any,
        path_params: dict[str, str],
        route_dep_plans: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve a WebSocket handler's plan.

        The `WebSocket` is passed where an HTTP resolve passes the `Request`, so
        the shared slot machinery serves both. Run teardowns with
        `run_teardowns()` once the handler returns.

        Args:
            plan: The handler's pre-built plan.
            websocket: The connection the parameters come from.
            path_params: Path parameters matched for the route.
            route_dep_plans: Plans of dependencies declared on the route itself.

        Returns:
            The keyword arguments to call the handler with.

        Raises:
            RequestValidationError: If a parameter is missing or fails validation.
            HTTPException: If a dependency or security scheme refuses the connection.

        """
        self.reset()

        if route_dep_plans:
            for slot in route_dep_plans:
                await self._exec_depends(slot, websocket, path_params)

        return await self._resolve_slots(plan, websocket, path_params)

    async def run_teardowns(self, exc: BaseException | None = None) -> None:
        """Run yield-dependency teardowns in reverse registration order.

        Each generator is advanced one step (or thrown into if *exc* is set) so
        the code after its ``yield`` executes. Every teardown runs even when an
        earlier one fails; the failures are logged, then re-raised together as a
        `BaseExceptionGroup` (PEP 654) on 3.11+, or singly on 3.10.

        Args:
            exc: The exception the handler raised, thrown into each generator so its
                `finally` and `except` blocks see it; `None` after a normal return.

        Raises:
            BaseExceptionGroup: If one or more teardowns failed (the first failure
                itself on 3.10).

        """
        failures: list[BaseException] = []
        # In reverse, matching `contextlib.ExitStack` semantics.
        while self._teardowns:
            kind, gen = self._teardowns.pop()
            try:
                if kind == "sync":
                    if exc is not None:
                        # Only the exhausted signal; a real bug propagates.
                        with contextlib.suppress(StopIteration):
                            gen.throw(exc)
                    else:
                        with contextlib.suppress(StopIteration):
                            next(gen)
                else:  # async
                    if exc is not None:
                        with contextlib.suppress(StopAsyncIteration):
                            await gen.athrow(exc)
                    else:
                        with contextlib.suppress(StopAsyncIteration):
                            await gen.__anext__()
            except Exception as teardown_exc:
                # The request exception re-emerging is not a teardown failure.
                if teardown_exc is exc:
                    continue
                _logger.exception("teardown raised")
                failures.append(teardown_exc)

        if not failures:
            return
            # The group keeps every individual traceback.
        if _BaseExceptionGroup is not None:
            raise _BaseExceptionGroup(
                "exceptions raised during dependency teardown", failures
            ) from exc
            # 3.10 has no exception groups; the rest are already logged.
        raise failures[0] from exc

    async def resolve(
        self,
        handler: Callable[..., Any],
        request: Request,
        path_params: dict[str, str],
        route_dependencies: list[Depends] | None = None,
    ) -> dict[str, Any]:
        """Build a plan on demand, for a caller that did not pre-plan.

        The back-compat path: tests and direct callers land here.

        Args:
            handler: The callable whose parameters are resolved.
            request: The request the parameters come from.
            path_params: Path parameters matched for the route.
            route_dependencies: Dependencies declared on the route itself.

        Returns:
            The keyword arguments to call `handler` with.

        Raises:
            RequestValidationError: If a parameter is missing or fails validation.
            HTTPException: If a dependency or security scheme refuses the request.

        """
        plan = build_plan(handler, path_params=frozenset(path_params))
        rdp = build_route_dep_plans(route_dependencies) if route_dependencies else None
        return await self.resolve_plan(plan, request, path_params, rdp)

    async def _resolve_slots(
        self,
        plan: Any,
        request: Request,
        path_params: dict[str, str],
    ) -> dict[str, Any]:
        """Walk a plan's slots in order, binding each handler kwarg by slot kind.

        The interpreter behind the codegen fast paths in `resolve_plan`; it runs
        only for the plans those reject - batched dependency waves, Security
        scopes, yield teardowns, overrides, an MCP context, or body models.
        """
        slots = plan.slots
        kwargs: dict[str, Any] = {}
        # `wave_trigger` maps the earliest batched index to the ordered waves.
        wave_trigger = plan.wave_trigger
        wave_members = plan.wave_members

        # Hoisted so the per-`K_QUERY` check below reads a local.
        mcp_context = self._mcp_context
        # `enumerate` measures ~7% faster than a manual counter here.
        for i, slot in enumerate(slots):
            kind = slot.kind
            name = slot.name

            # ── Framework injections ──────────────────────────────
            if kind == K_REQUEST:
                kwargs[name] = request
                continue

            if kind == K_WEBSOCKET:
                kwargs[name] = request
                continue

            if kind == K_BG_TASKS:
                kwargs[name] = self._bind_background_tasks(request)
                continue

            if kind == K_RESPONSE:
                kwargs[name] = self._bind_injected_response(request)
                continue

            if kind == K_SECURITY_SCOPES:
                # `__init__` snapshots with `list(...)`, so no copy is needed.
                kwargs[name] = SecurityScopes(self._scope_stack)
                continue

            # ── Dependencies ──────────────────────────────────────
            if kind == K_DEPENDS:
                # Topological batching (see `compute_dep_waves`), so deps at different
                # depths run concurrently; an unsafe one falls through in slot order.
                if wave_members and i in wave_members:
                    # Only the earliest index carries waves.
                    waves = wave_trigger.get(i)
                    if waves is not None:
                        await self._run_dep_waves(waves, slots, request, path_params, kwargs)
                    continue
                kwargs[name] = await self._exec_depends(slot, request, path_params)
                continue

            # ── Markers and request body ──────────────────────────
            if kind == K_PARAM_MARKER:
                kwargs[name] = await self._resolve_marker(slot, request, path_params)
                continue

            if kind == K_BODY_MODEL:
                kwargs[name] = await self._resolve_body_model(slot, request)
                continue

            if kind == K_MODEL_GROUP:
                kwargs[name] = await self._resolve_model_group(slot, request)
                continue

            if kind == K_UPLOAD_FILE:
                await self._resolve_upload_file(slot, request, kwargs)
                continue

            # ── Path and query parameters ─────────────────────────
            if kind == K_QUERY_LIST:
                kwargs[name] = _resolve_list_param(slot, request, path_params)
                continue

            if kind == K_QUERY:
                # An `MCPContext` parameter lands here, being neither `Request` nor a model.
                if mcp_context is not None and slot.target_type is type(mcp_context):
                    kwargs[name] = mcp_context
                    continue
                kwargs[name] = _resolve_scalar_param(slot, request, path_params)
                continue

            # Fall-through: an unrecognised kind binds nothing.

        return kwargs

    async def _run_dep_waves(
        self,
        waves: list[list[int]],
        slots: list[Any],
        request: Request,
        path_params: dict[str, str],
        kwargs: dict[str, Any],
    ) -> None:
        """Resolve batched dependency waves concurrently into `kwargs`.

        Each wave is gathered and written before the next runs, so a wave that
        reuses a cached result always follows the wave that filled it (see
        `compute_dep_waves`).
        """
        for wave in waves:
            group = [slots[w] for w in wave]
            results = await asyncio.gather(
                *(self._exec_depends(s, request, path_params) for s in group)
            )
            for s, r in zip(group, results, strict=True):
                kwargs[s.name] = r

    @staticmethod
    def _bind_background_tasks(request: Request) -> BackgroundTasks:
        """Lazily attach and return the request's `BackgroundTasks` queue."""
        if request._background_tasks is None:
            request._background_tasks = BackgroundTasks()
        return request._background_tasks

    @staticmethod
    def _bind_injected_response(request: Request) -> Response:
        """Lazily create and return the per-request injected `Response`.

        One per request, shared with any dependency that also declares it.
        `status_code = 0` is the "not set by the handler" sentinel.
        """
        injected = request._state.get(STATE_INJECTED_RESPONSE)
        if injected is None:
            injected = Response()
            injected.status_code = 0
            request._state[STATE_INJECTED_RESPONSE] = injected
        return injected

    async def _resolve_marker(
        self, slot: Any, request: Request, path_params: dict[str, str]
    ) -> Any:
        marker = slot.marker
        mk = slot.marker_kind
        lookup = slot.lookup_name

        # A list-typed marker collects every repeated value, not just the first.
        if mk in (0, 2, 3, 5) and slot.marker_reads_list:
            if mk == 5:  # MK_FORM
                values = (await request.form()).getlist(lookup)
            elif mk == 2:  # MK_HEADER
                values = _every_value(request.headers, lookup.lower())
            elif mk == 3:  # MK_COOKIE
                values = _every_value(request.cookies, lookup)
            else:  # MK_QUERY
                values = request.query_params.getlist(lookup)
            loc = MARKER_LOC[mk]
            if not values:
                if marker.has_default:
                    return marker.resolve_default()
                if slot.is_optional:
                    return None
                raise _err_missing_marker(loc, slot.name)
            collected = _collect_sequence_marker(values, slot.target_type, slot.name, loc)
            if slot.list_checks is not None:
                _validate_list_marker(collected, slot.list_checks, slot.name, loc)
            return collected

        if mk == 1:  # MK_PATH
            raw = path_params.get(lookup)
        elif mk == 2:  # MK_HEADER
            # Single-header read; see `Request._peek_header`.
            raw = request._peek_header(lookup.lower())
        elif mk == 3:  # MK_COOKIE
            raw = request.cookies.get(lookup)
        elif mk == 4:  # MK_BODY
            # An endpoint parsing `text/plain` is reachable cross-origin unpreflighted.
            self._require_json_body(request)
            body = await request.json()
            # `Body(embed=True)`: under the param name, not the whole body -
            # but only when the body is actually a dict; a bare array sent
            # to an embedded param was never under that key to begin with.
            embedded = marker.embed and isinstance(body, dict)
            raw = body.get(lookup) if embedded else body
            # `marker.validate` bounds a scalar, so an array is checked here.
            if slot.marker_reads_list and isinstance(raw, list):
                # The element type first, or a raw `["0"]` skips `items.minimum`.
                items = _validate_body_list(raw, slot, embedded)
                if slot.list_checks is not None:
                    _validate_list_marker(items, slot.list_checks, slot.name, MARKER_LOC[mk])
                return items
        elif mk in (5, 6):  # MK_FORM / MK_FILE
            form = await request.form()
            raw = form.get(lookup)
        else:  # MK_QUERY (0)
            raw = request.query_params.get(lookup)

        loc = MARKER_LOC[mk]
        if raw is None:
            if marker.has_default:
                return marker.resolve_default()
            if slot.is_optional:
                return None
            raise _err_missing_marker(loc, slot.name)

            # `_coerce_scalar` would pass a `dict` straight through.
        if slot.model is not None:
            return self._validate_marker_body(raw, slot)

        raw = _coerce_scalar(raw, slot.target_type, slot.name, loc)

        try:
            return marker.validate(raw, slot.name)
        except ValueError as e:
            raise RequestValidationError(
                [{"loc": [loc, slot.name], "msg": str(e), "type": "value_error"}]
            ) from e

    async def _exec_depends(self, slot: Any, request: Request, path_params: dict[str, str]) -> Any:
        """Resolve a K_DEPENDS slot with caching and override support."""
        # One `_Slot` serves every kind, so the field is declared optional; a
        # K_DEPENDS slot always carries it. Annotated rather than cast: a local
        # annotation narrows the reads below and is not evaluated at runtime.
        dep_callable: Callable[..., Any] = slot.dep_callable
        actual = self._overrides.get(dep_callable, dep_callable)

        # Plain `Depends` has none, so every scope branch below is skipped.
        new_scopes: list[str] = slot.target_type if isinstance(slot.target_type, list) else []

        # Keyed on the callable plus the scope union, so two scope sets cannot collide.
        if slot.scope_sensitive:
            cache_key: Any = (actual, frozenset(self._scope_stack) | frozenset(new_scopes))
        else:
            cache_key = actual

        if slot.use_cache and cache_key in self._cache:
            return self._cache[cache_key]

        if actual is dep_callable:
            sub_plan = slot.sub_plan
            is_coro = slot.dep_is_coro
            is_gen = slot.dep_is_gen
            is_async_gen = slot.dep_is_async_gen
        else:
            # Memoised on the override callable, shared across requests.
            entry = self._override_subplans.get(actual)
            if entry is None:
                entry = (
                    build_plan(actual),
                    _is_async_callable(actual),
                    inspect.isgeneratorfunction(actual),
                    inspect.isasyncgenfunction(actual),
                )
                # A non-weak-referenceable target skips caching: re-probing is fine,
                # leaking is not. The sentinel is swapped for a per-resolver map first.
                if self._override_subplans is _EMPTY_OVERRIDE_SUBPLANS:
                    self._override_subplans = weakref.WeakKeyDictionary()
                with contextlib.suppress(TypeError):
                    self._override_subplans[actual] = entry
            sub_plan, is_coro, is_gen, is_async_gen = entry

            # Pushed for a `SecurityScopes` slot in the sub-plan, popped either way.
        if new_scopes:
            self._scope_stack.extend(new_scopes)
        try:
            sub_kwargs = await self._resolve_slots(sub_plan, request, path_params)
        finally:
            if new_scopes:
                del self._scope_stack[-len(new_scopes) :]

        if is_gen:
            # Onto the teardown stack, so `run_teardowns` advances past the yield.
            gen = actual(**sub_kwargs)
            try:
                result = next(gen)
            except StopIteration as err:
                raise RuntimeError(MSG_YIELD_NO_VALUE.format(dependency=actual)) from err
            self._teardowns.append(("sync", gen))
        elif is_async_gen:
            agen = actual(**sub_kwargs)
            try:
                result = await agen.__anext__()
            except StopAsyncIteration as err:
                raise RuntimeError(MSG_YIELD_NO_VALUE.format(dependency=actual)) from err
            self._teardowns.append(("async", agen))
        elif is_coro:
            result = await actual(**sub_kwargs)
        elif slot.dep_offload:
            # `offload` preserves request-scoped ContextVars.
            result = await offload(actual, **sub_kwargs)
        else:
            result = actual(**sub_kwargs)

        if slot.use_cache:
            self._cache[cache_key] = result
        return result
