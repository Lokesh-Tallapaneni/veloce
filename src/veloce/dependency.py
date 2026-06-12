"""Dependency injection — pre-planned at registration, executed per request.

Public API: `Depends`, `Security`, `SecurityScopes`. The resolver walks a
pre-built `HandlerPlan` (see `veloce._handler_plan`) rather than reflecting on
the handler signature per request.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import functools
import inspect
import logging
import weakref
from collections.abc import Callable
from enum import Enum
from typing import Annotated, Any, Literal, get_args, get_origin

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from typing_extensions import Doc

from veloce._constants import MSG_FIELD_REQUIRED
from veloce._handler_plan import (
    K_BG_TASKS,
    K_BODY_MODEL,
    K_DEPENDS,
    K_PARAM_MARKER,
    K_PATH,
    K_QUERY,
    K_QUERY_LIST,
    K_REQUEST,
    K_RESPONSE,
    K_SECURITY_SCOPES,
    K_UPLOAD_FILE,
    K_WEBSOCKET,
    MARKER_LOC,
    _slot_parallel_safe,
    parallel_group_end,
)
from veloce._internal import _is_async_callable, offload
from veloce._model_backend import ModelBackend, is_pydantic_model
from veloce._resolver_codegen import compile_graph_resolver, compile_param_resolver
from veloce.background import BackgroundTasks
from veloce.exceptions import RequestValidationError, ValidationError
from veloce.http.request import Request
from veloce.http.response import Response

_logger = logging.getLogger(__name__)

# `BaseExceptionGroup` is a builtin only from Python 3.11 (PEP 654); on 3.10 the
# name is absent. When several yield-dependency teardowns fail, the failures are
# surfaced together as a group on 3.11+ and chained singly on 3.10.
_BaseExceptionGroup: type[BaseException] | None = getattr(builtins, "BaseExceptionGroup", None)

# Marks a plan whose compiled-resolver build was attempted and rejected, so
# resolve_plan does not retry compilation on every request.
_NOT_COMPILABLE = object()

# Shared empty sentinels for resolvers that never see app-level overrides
# (the dispatcher overwrites the slot before any read, so the sentinels
# never appear on the dispatch path). Module-level so they are not
# reallocated per request. `_EMPTY_OVERRIDES` is only ever `.get(...)`'d
# by `_exec_depends`, so it stays untouched. `_EMPTY_OVERRIDE_SUBPLANS`
# is read AND written by `_exec_depends`; the writer swaps the slot to a
# fresh `WeakKeyDictionary` on the first write so the sentinel is never
# mutated and cross-resolver contamination is impossible for direct
# `DependencyResolver()` callers (tests, public-API users).
_EMPTY_OVERRIDES: dict[Callable, Callable] = {}
_EMPTY_OVERRIDE_SUBPLANS: weakref.WeakKeyDictionary[Callable, Any] = weakref.WeakKeyDictionary()


# -- Helpers -----------------------------------------------


@functools.lru_cache(maxsize=512)
def _type_adapter(target_type: Any) -> TypeAdapter | None:
    """Build (once) and cache a Pydantic ``TypeAdapter`` for a parameter type.

    Adapters are keyed on the annotation object, so a route that declares
    `created: datetime = Query(...)` pays the adapter-construction cost once
    at first request, never again. ``None`` - the result for an annotation
    Pydantic cannot build an adapter for - is cached too, so an un-adaptable
    type is not re-attempted (and re-failed) on every subsequent request.
    """
    try:
        return TypeAdapter(target_type)
    except Exception:
        return None


def _coerce_literal(value: Any, target_type: Any, param_name: str, loc: str) -> Any:
    """Match a request value against a `Literal[...]` parameter.

    Request values arrive as strings, so Pydantic's strict literal check
    rejects an `int` / `bool` literal outright. Each plausible coercion of
    the value - the raw string, a bool, an int, a float - is compared
    against the literal members by *exact* runtime type, sidestepping
    Python's loose `1 == True` equivalence.
    """
    members = get_args(target_type)
    candidates: list[Any] = [value]
    if isinstance(value, str):
        candidates.append(value.lower() in ("true", "1", "yes"))
        with contextlib.suppress(ValueError):
            candidates.append(int(value))
        with contextlib.suppress(ValueError):
            candidates.append(float(value))
    for member in members:
        # An `Enum` member is matched against its underlying value, so a
        # `str` / `int`-backed enum member still accepts the plain request
        # value while the original member object is what gets returned.
        comparand = member.value if isinstance(member, Enum) else member
        for candidate in candidates:
            if type(candidate) is type(comparand) and candidate == comparand:
                return member
    raise RequestValidationError(
        [
            {
                "loc": [loc, param_name],
                "msg": f"Input should be one of: {', '.join(repr(m) for m in members)}",
                "type": "literal_error",
            }
        ]
    )


def _is_model_typed(target_type: Any) -> bool:
    """Return True for a Pydantic ``BaseModel`` subclass annotation."""
    return is_pydantic_model(target_type)


def _coerce_via_pydantic(value: Any, target_type: Any, param_name: str, loc: str) -> Any:
    """Validate and coerce a request value through Pydantic.

    The fast scalar branches of `_coerce_value` cover `str` / `int` / `float`
    / `bool` / `Enum`; every other annotation - `datetime`, `date`, `time`,
    `UUID`, `Decimal`, `Path`, constrained generics - is handled here so
    query / path / header / cookie parameters get the same full validation a
    request-body model already enjoys.

    If Pydantic cannot build an adapter for the annotation the raw value is
    returned unchanged, preserving the previous pass-through behaviour.
    """
    try:
        adapter = _type_adapter(target_type)
    except TypeError:
        # Unhashable annotation - `lru_cache` cannot key it. Fall back to
        # the raw value, the behaviour before Pydantic coercion existed.
        return value
    if adapter is None:
        return value
    try:
        # A model-typed non-body / form param arrives as a raw string that
        # the caller has serialised as a JSON document (`?tag={"name":"x"}`).
        # `validate_python` would reject the string with a `model_type`
        # error, so parse the JSON form into the model instead. Scalars and
        # already-structured values keep `validate_python`.
        if isinstance(value, str) and _is_model_typed(target_type):
            return adapter.validate_json(value)
        return adapter.validate_python(value)
    except PydanticValidationError as err:
        raise RequestValidationError(
            [
                {
                    "loc": [loc, param_name],
                    "msg": e["msg"],
                    "type": e["type"],
                }
                for e in err.errors()
            ]
        ) from err


def _coerce_value(value: Any, target_type: Any, param_name: str, loc: str) -> Any:
    """Coerce a string value to the target type."""
    if target_type is str or target_type is Any or target_type is None:
        return value
    try:
        if target_type is int:
            return int(value)
        if target_type is float:
            return float(value)
        if target_type is bool:
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes")
            return bool(value)
        # Enum (or any class with __members__).
        if hasattr(target_type, "__members__"):
            try:
                return target_type(value)
            except (ValueError, KeyError) as err:
                valid = list(target_type.__members__.keys())
                raise RequestValidationError(
                    [
                        {
                            "loc": [loc, param_name],
                            "msg": f"Invalid value for {param_name}: must be one of {valid}",
                            "type": "type_error",
                        }
                    ]
                ) from err
    except (ValueError, TypeError) as err:
        raise RequestValidationError(
            [
                {
                    "loc": [loc, param_name],
                    "msg": f"Invalid value for {param_name}: expected {getattr(target_type, '__name__', target_type)}",
                    "type": "type_error",
                }
            ]
        ) from err

    # No fast scalar branch matched. `Literal[...]` needs string-aware
    # member matching; everything else goes to Pydantic for full coverage.
    if get_origin(target_type) is Literal:
        return _coerce_literal(value, target_type, param_name, loc)
    return _coerce_via_pydantic(value, target_type, param_name, loc)


def _coerce_scalar(value: Any, target_type: Any, param_name: str, loc: str) -> Any:
    """Coerce `value` to `target_type` unless it is a string or already structured.

    The guard shared by the marker and bare-query resolution tails (HTTP and the
    MCP argument binder): a `str` target, an absent target, or an already-decoded
    `dict` / `list` needs no scalar coercion; anything else goes through
    `_coerce_value`.
    """
    if target_type and target_type is not str and not isinstance(value, (dict, list)):
        return _coerce_value(value, target_type, param_name, loc)
    return value


def _err_missing_marker(loc: str, name: str) -> RequestValidationError:
    """Build the missing-required error for a `Query`/`Header`/`Cookie`/`Form`
    marker. The list and scalar branches of `_resolve_marker` raise the same
    `value_error.missing` shape, so it is constructed in one place. (A bare
    `K_QUERY` slot uses the distinct `'missing'` / `MSG_FIELD_REQUIRED` contract;
    that intentional difference is preserved.)
    """
    return RequestValidationError(
        [
            {
                "loc": [loc, name],
                "msg": f"Missing required parameter: {name}",
                "type": "value_error.missing",
            }
        ]
    )


# -- Markers -----------------------------------------------


class Depends:
    """Dependency marker — use in function signature defaults.

    `dependency` may be omitted (`Depends()`); the resolver then infers
    it from the parameter's type annotation — the shorthand for
    `x: SomeClass = Depends()`.

    Usage::

        def get_db() -> Database:
            return Database()

        @app.get("/users")
        async def list_users(db: Database = Depends(get_db)) -> list[str]:
            return db.all_usernames()
    """

    __slots__ = ("dependency", "use_cache", "offload")

    def __init__(
        self,
        dependency: Annotated[
            Callable | None,
            Doc(
                "Callable to resolve and inject; inferred from the parameter's type annotation when omitted."
            ),
        ] = None,
        use_cache: Annotated[
            bool,
            Doc(
                "Cache the resolved value so a dependency referenced more than once per request runs once."
            ),
        ] = True,
        offload: Annotated[
            bool,
            Doc(
                "Run a blocking sync dependency in the thread pool so it cannot stall the event loop."
            ),
        ] = False,
    ) -> None:
        self.dependency = dependency
        self.use_cache = use_cache
        # When a sync dependency does blocking work (a DB driver call, a
        # `requests.get`), running it inline on the event loop stalls every
        # other in-flight request on this worker. `offload=True` opts that
        # one dependency into the thread pool, mirroring how sync route
        # handlers are already offloaded. Defaults off so trivial pure
        # functions keep their zero-overhead inline call.
        self.offload = offload


class Security(Depends):
    """Dependency marker with OAuth2 scopes for OpenAPI emission."""

    __slots__ = ("scopes",)

    def __init__(
        self,
        dependency: Annotated[
            Callable | None,
            Doc(
                "Callable to resolve and inject; inferred from the parameter's type annotation when omitted."
            ),
        ] = None,
        scopes: Annotated[
            list[str] | None,
            Doc(
                "OAuth 2.0 scopes this dependency requires, accumulated for the `SecurityScopes` chain and OpenAPI."
            ),
        ] = None,
        use_cache: Annotated[
            bool,
            Doc(
                "Cache the resolved value so a dependency referenced more than once per request runs once."
            ),
        ] = True,
        offload: Annotated[
            bool,
            Doc(
                "Run a blocking sync dependency in the thread pool so it cannot stall the event loop."
            ),
        ] = False,
    ) -> None:
        super().__init__(dependency=dependency, use_cache=use_cache, offload=offload)
        self.scopes = scopes or []


class SecurityScopes:
    """Aggregated OAuth 2.0 scopes for the current Security() chain.

    A handler / sub-dependency that declares a parameter of this type
    receives the union of all `Security(..., scopes=[...])` calls between
    the route entry and this point in the dependency graph. Typical use:
    an authorising dependency checks `security_scopes.scopes` against
    the scopes the bearer token actually carries and builds a
    `WWW-Authenticate: Bearer scope="<...>"` header when denying.

    Per RFC 6749 Sec. 3.3 the scope-string serialisation is space-separated.
    """

    __slots__ = ("scopes", "scope_str")

    def __init__(self, scopes: list[str] | None = None) -> None:
        self.scopes: list[str] = list(scopes) if scopes else []
        self.scope_str: str = " ".join(self.scopes)

    def __repr__(self) -> str:
        return f"SecurityScopes({self.scopes!r})"


# -- Resolver ----------------------------------------------

# Returned by `_resolve_scalar_param` for a path slot with no value, no default,
# and not optional: the caller leaves the kwarg unset so the handler default
# applies. A query slot raises `missing` instead, so it never returns this.
_PARAM_MISSING: Any = object()


def _resolve_scalar_param(
    slot: Any, request: Request, path_params: dict[str, str], *, allow_query: bool
) -> Any:
    """Resolve a scalar path-or-query parameter to its coerced value.

    A path binding wins when the matched params include the name; a `K_QUERY`
    slot (`allow_query=True`) then falls back to the query string, a `K_PATH`
    slot does not. With no value, a default or optional yields that; otherwise a
    query slot raises `missing` and a path slot returns `_PARAM_MISSING`.
    """
    name = slot.name
    if name in path_params:
        return _coerce_value(path_params[name], slot.target_type or str, name, "path")
    if allow_query and name in request.query_params:
        return _coerce_value(request.query_params[name], slot.target_type or str, name, "query")
    if slot.has_default:
        # A plain mutable default is wrapped in a copying factory at
        # registration so each request gets its own value (see
        # `_guard_plain_mutable_default`); immutable defaults read inline.
        if slot.default_factory is not None:
            return slot.default_factory()
        return slot.default
    if slot.is_optional:
        return None
    if allow_query:
        raise RequestValidationError(
            [{"loc": ("query", name), "msg": MSG_FIELD_REQUIRED, "type": "missing"}]
        )
    return _PARAM_MISSING


def _resolve_list_param(slot: Any, request: Request, path_params: dict[str, str]) -> Any:
    """Resolve a list-typed path-or-query parameter to a coerced list.

    A single path value becomes a one-element list; a query key yields every
    value the URL carried (`?tag=a&tag=b` -> ["a", "b"]). Falls back to the
    default / optional, else raises `missing`.
    """
    name = slot.name
    if name in path_params:
        return [_coerce_value(path_params[name], slot.list_inner, name, "path")]
    if name in request.query_params:
        # MultiDict.getall returns every value the URL carried for this key.
        return [
            _coerce_value(v, slot.list_inner, name, "query")
            for v in request.query_params.getall(name)
        ]
    if slot.has_default:
        # A plain mutable default is wrapped in a copying factory at
        # registration so each request gets its own value (see
        # `_guard_plain_mutable_default`); immutable defaults read inline.
        if slot.default_factory is not None:
            return slot.default_factory()
        return slot.default
    if slot.is_optional:
        return None
    raise RequestValidationError(
        [{"loc": ("query", name), "msg": MSG_FIELD_REQUIRED, "type": "missing"}]
    )


class DependencyResolver:
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
        self._cache: dict[Callable, Any] = {}
        # `_overrides` and `_override_subplans` are immediately overwritten
        # by `Veloce._dispatch_request` with the app-scoped instances. Skip
        # the throwaway dict + WeakKeyDictionary allocation here on the hot
        # path; the module-level sentinels below provide the same
        # iteration / `.get(...)` semantics for resolvers constructed
        # outside the dispatcher (unit tests, direct callers).
        self._overrides: dict[Callable, Callable] = _EMPTY_OVERRIDES
        self._override_subplans: weakref.WeakKeyDictionary[Callable, Any] = _EMPTY_OVERRIDE_SUBPLANS
        # Per-request stack of yield-style dependency teardowns. Each entry is
        # `(kind, generator)` where kind is "sync" or "async". The stack is
        # drained in reverse by `run_teardowns()` after the response.
        self._teardowns: list[tuple[str, Any]] = []
        # Per-request accumulator of `Security(..., scopes=[...])` scopes.
        # Pushed before a Security sub-plan resolves, popped after. A
        # `SecurityScopes` parameter inside the chain reads this stack.
        self._scope_stack: list[str] = []
        # The MCP tool-call context to inject into any sub-dependency that
        # declares a parameter typed `MCPContext` (set by the MCP bridge before
        # resolution; `None` on the HTTP / WebSocket paths). It is bound the way
        # a WebSocket is bound into a sub-dep - by the slot's declared TYPE -
        # so a `MCPContext`-typed slot anywhere in the graph receives it.
        self._mcp_context: Any = None

    def reset(self) -> None:
        """Clear the per-request resolver state.

        Run at the top of every `resolve_plan`, and also called directly
        by the dispatcher's trivial-route fast path (a route with no
        parameters and no dependencies) so the shared resolver never
        carries a previous request's cached results into the next one.
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
        """Fast path - consume a pre-built `HandlerPlan`."""
        # Clear per-request state on every call. DependencyResolver is public
        # and a caller may reuse one instance across resolves, so a prior
        # resolve's cache / teardown stack / scope stack must never leak into
        # this one - including into the compiled fast path below.
        self.reset()

        # Param-only plans (request + scalar path/query, no route deps) resolve
        # through a straight-line function compiled on first use and cached on
        # the plan. It reads no resolver state, so after the reset above it
        # returns directly without entering the interpreter loop. A plan the
        # compiler rejects (`_NOT_COMPILABLE`) falls through to the interpreter.
        if not route_dep_plans:
            cr = plan.compiled_resolver
            if cr is None:
                compiled = compile_param_resolver(plan, _coerce_value, RequestValidationError)
                plan.compiled_resolver = cr = compiled if compiled is not None else _NOT_COMPILABLE
            if cr is not _NOT_COMPILABLE:
                return cr(request, path_params)

            # The param-only compiler rejected this plan (it has dependencies or
            # other interpreter-only slots). A no-wave dependency graph - a
            # linear chain with no parallel-safe batching to preserve, no
            # Security scopes, and no yield-teardown deps - compiles to a
            # straight-line `async` resolver too. It reads neither the override
            # map nor the MCP context, so it is used only when both are absent;
            # an active override or MCP context falls through to the interpreter,
            # which applies them. The compiled body is self-contained (its own
            # locals dedup shared deps), so it runs after the `reset()` above
            # without touching `_cache` / `_teardowns` / `_scope_stack`.
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
                    )
                    plan.compiled_graph_resolver = gcr = (
                        graph if graph is not None else _NOT_COMPILABLE
                    )
                if gcr is not _NOT_COMPILABLE:
                    return await gcr(request, path_params)
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

        The shared slot machinery is reused - the `WebSocket` is passed
        where an HTTP resolve passes the `Request`, so `K_WEBSOCKET` slots,
        the `Depends` graph (with `yield`-teardown and `Security` scope
        accumulation), and `path` parameters all resolve through one code
        path. Run teardowns with `run_teardowns()` once the handler returns.
        """
        self.reset()

        if route_dep_plans:
            for slot in route_dep_plans:
                await self._exec_depends(slot, websocket, path_params)

        return await self._resolve_slots(plan, websocket, path_params)

    async def run_teardowns(self, exc: BaseException | None = None) -> None:
        """Run yield-dependency teardowns in reverse registration order.

        Each generator is advanced one step (or thrown into if *exc* is set)
        so the code after its ``yield`` executes. Every teardown runs even when
        an earlier one fails; the failures are collected and re-raised together
        after the whole chain has drained, so a failing teardown (a transaction
        commit / rollback error, say) is observable rather than silently
        swallowed. Each is logged as it happens to preserve the previous
        observability, then surfaced as a `BaseExceptionGroup` (PEP 654) on
        3.11+ - chained from *exc* when the request itself failed - or as a
        single chained raise on 3.10.
        """
        # Collected teardown failures. Only genuine teardown bugs land here;
        # the request exception re-emerging from `gen.throw(exc)` is the error
        # the caller already holds, so it is not double-counted.
        failures: list[BaseException] = []
        # Drain in reverse so the most recently set-up dependency tears down
        # first - matches Python's contextlib.ExitStack semantics.
        while self._teardowns:
            kind, gen = self._teardowns.pop()
            try:
                if kind == "sync":
                    if exc is not None:
                        # Only swallow the generator-exhausted signal; real
                        # teardown bugs propagate to the outer except so they
                        # get logged and collected instead of disappearing.
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
                # The request exception thrown into the generator re-emerging
                # unchanged is not a teardown failure - it is the error the
                # caller is already handling. Skip it so it is not aggregated.
                if teardown_exc is exc:
                    continue
                _logger.exception("teardown raised")
                failures.append(teardown_exc)

        if not failures:
            return
        # Surface the collected failures. The group keeps every individual
        # traceback (vs. the previous swallow) and chains from the request
        # error when there was one.
        if _BaseExceptionGroup is not None:
            raise _BaseExceptionGroup(
                "exceptions raised during dependency teardown", failures
            ) from exc
        # 3.10 has no exception groups: re-raise the first failure (already
        # logged the rest) chained from the request error so it is observable.
        raise failures[0] from exc

    async def resolve(
        self,
        handler: Callable,
        request: Request,
        path_params: dict[str, str],
        route_dependencies: list[Depends] | None = None,
    ) -> dict[str, Any]:
        """Back-compat path - build a plan on demand. Tests and direct
        callers that did not pre-plan land here.
        """
        from veloce._handler_plan import build_plan, build_route_dep_plans

        plan = build_plan(handler)
        rdp = build_route_dep_plans(route_dependencies) if route_dependencies else None
        return await self.resolve_plan(plan, request, path_params, rdp)

    async def _resolve_slots(
        self,
        plan: Any,
        request: Request,
        path_params: dict[str, str],
    ) -> dict[str, Any]:
        """Walk a plan's slots in order, binding each handler kwarg by slot kind.

        The interpreter that backs the codegen fast paths (trivial / request-only
        / `compile_param_resolver` / `compile_graph_resolver` in `resolve_plan`);
        it runs only for plans those reject - batched dependency waves, Security
        scopes, yield teardowns, overrides, an MCP context, or body models. Each
        kind binds through a named helper, so adding a kind is one branch plus one
        binder. The map, by group:

        - framework injections: `K_REQUEST` / `K_WEBSOCKET` (the connection),
          `K_BG_TASKS` -> `_bind_background_tasks`, `K_RESPONSE` ->
          `_bind_injected_response`, `K_SECURITY_SCOPES` (the live scope stack);
        - dependencies: `K_DEPENDS` -> `_run_dep_waves` (batched) or
          `_exec_depends` (inline, in slot order);
        - markers and body: `K_PARAM_MARKER` -> `_resolve_marker`, `K_BODY_MODEL`
          -> `_resolve_body_model`, `K_UPLOAD_FILE` -> `_resolve_upload_file`;
        - path / query parameters: `K_QUERY_LIST` -> `_resolve_list_param`,
          `K_QUERY` / `K_PATH` -> `_resolve_scalar_param`.
        """
        slots = plan.slots
        kwargs: dict[str, Any] = {}
        # Precomputed at registration. `wave_trigger` maps the earliest batched
        # slot index to the ordered list of waves; `wave_members` holds every
        # batched index. Both are empty when there is nothing to batch, so a
        # linear chain pays no per-request overhead.
        wave_trigger = plan.wave_trigger
        wave_members = plan.wave_members

        i = 0
        n = len(slots)
        # Hoist the MCP tool-call context to a local. It is `None` on every
        # HTTP and WebSocket request (only the MCP bridge sets it), so the
        # per-`K_QUERY`-slot binding check below reads a local instead of doing
        # an attribute load each iteration on the dependency hot path.
        mcp_context = self._mcp_context
        while i < n:
            slot = slots[i]
            kind = slot.kind
            name = slot.name

            # ── Framework injections ──────────────────────────────
            if kind == K_REQUEST:
                kwargs[name] = request
                i += 1
                continue

            if kind == K_WEBSOCKET:
                # WebSocket plans pass the connection where an HTTP resolve
                # passes the `Request`, so the same object is bound here.
                kwargs[name] = request
                i += 1
                continue

            if kind == K_BG_TASKS:
                kwargs[name] = self._bind_background_tasks(request)
                i += 1
                continue

            if kind == K_RESPONSE:
                kwargs[name] = self._bind_injected_response(request)
                i += 1
                continue

            if kind == K_SECURITY_SCOPES:
                # `SecurityScopes.__init__` already snapshots its argument with
                # `list(...)`, so pass the live stack directly rather than
                # copying it twice; later mutations still don't affect this
                # instance.
                kwargs[name] = SecurityScopes(self._scope_stack)
                i += 1
                continue

            # ── Dependencies ──────────────────────────────────────
            if kind == K_DEPENDS:
                # Topological batching (see `compute_dep_waves`): every batched
                # parallel-safe dep is resolved at the earliest batched slot,
                # so deps separated by non-dependency slots or sitting at
                # different nesting depths still run concurrently. Waves run
                # sequentially (each gathered, then awaited) so a wave that
                # reuses a cached result always follows the wave that filled it.
                # A batched member reached later is already in `kwargs` and is
                # skipped. Unsafe deps (Security() scope mutators, yield deps)
                # are never batched; they fall through to the inline resolve
                # below in slot order, preserving teardown and scope semantics.
                if wave_members and i in wave_members:
                    # `wave_trigger` only holds the earliest batched index; at
                    # any later batched member the result is already in `kwargs`
                    # and there is nothing to do but advance.
                    waves = wave_trigger.get(i)
                    if waves is not None:
                        await self._run_dep_waves(waves, slots, request, path_params, kwargs)
                    i += 1
                    continue
                kwargs[name] = await self._exec_depends(slot, request, path_params)
                i += 1
                continue

            # ── Markers and request body ──────────────────────────
            if kind == K_PARAM_MARKER:
                kwargs[name] = await self._resolve_marker(slot, request, path_params)
                i += 1
                continue

            if kind == K_BODY_MODEL:
                kwargs[name] = await self._resolve_body_model(slot, request)
                i += 1
                continue

            if kind == K_UPLOAD_FILE:
                await self._resolve_upload_file(slot, request, kwargs)
                i += 1
                continue

            # ── Path and query parameters ─────────────────────────
            if kind == K_QUERY_LIST:
                kwargs[name] = _resolve_list_param(slot, request, path_params)
                i += 1
                continue

            if kind == K_QUERY:
                # An MCP `MCPContext`-typed parameter lands on a K_QUERY slot
                # (it is neither `Request` nor a model). When the resolver is
                # driving an MCP tool call, bind the context here so a
                # sub-dependency typed `MCPContext` receives it - mirroring how
                # a WebSocket is injected into sub-deps by declared type. The
                # type identity check keeps a plain `ctx: str` query parameter
                # an ordinary agent input.
                if mcp_context is not None and slot.target_type is type(mcp_context):
                    kwargs[name] = mcp_context
                    i += 1
                    continue
                kwargs[name] = _resolve_scalar_param(slot, request, path_params, allow_query=True)
                i += 1
                continue

            # K_PATH is not currently emitted by build_plan; future-proof.
            if kind == K_PATH:
                val = _resolve_scalar_param(slot, request, path_params, allow_query=False)
                if val is not _PARAM_MISSING:
                    kwargs[name] = val
                i += 1
                continue

            # Fall-through: kind didn't match anything; advance so the
            # loop terminates instead of spinning on an unknown slot.
            i += 1

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

        Each wave is gathered, then its results are written, before the next wave
        runs, so a wave that reuses a cached result always follows the wave that
        filled it (see `compute_dep_waves`). Entered only when a plan has batched
        parallel-safe deps; a linear chain never reaches here.
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

        One `Response` per request, shared between the handler and any dependency
        that also declares the parameter. `status_code = 0` is the "not set by the
        handler" sentinel the dispatcher checks before merging.
        """
        injected = request._state.get("_injected_response")
        if injected is None:
            injected = Response()
            injected.status_code = 0
            request._state["_injected_response"] = injected
        return injected

    async def _resolve_upload_file(
        self, slot: Any, request: Request, kwargs: dict[str, Any]
    ) -> None:
        """Bind an uploaded file from the multipart form, if present.

        Binds the upload when the field is present, `None` when it is absent and
        optional, and leaves the kwarg unset otherwise so the handler default
        applies.
        """
        upload = (await request.form()).get(slot.name)
        if upload is not None:
            kwargs[slot.name] = upload
        elif slot.is_optional:
            kwargs[slot.name] = None

    def _parallel_dep_group_end(self, slots: list[Any], start: int) -> int:
        """Compat shim. The grouping is precomputed at registration
        (`HandlerPlan.parallel_groups`); this delegates to the shared
        implementation for direct callers and tests.
        """
        return parallel_group_end(slots, start)

    def _slot_safe_for_parallel(self, slot: Any, seen_plans: set[int]) -> bool:
        """Compat shim delegating to the shared parallel-safety check."""
        return _slot_parallel_safe(slot, seen_plans)

    async def _resolve_body_model(self, slot: Any, request: Request) -> Any:
        if slot.backend == ModelBackend.MSGSPEC:
            return await self._resolve_msgspec_body(slot, request)
        return await self._resolve_pydantic_body(slot, request)

    async def _resolve_pydantic_body(self, slot: Any, request: Request) -> Any:
        try:
            body_data = await request.json()
            return slot.model.model_validate(body_data)
        except PydanticValidationError as e:
            # Prefix each field path with "body" so a single body model's errors
            # carry the same `["body", ...]` location as a `Body(...)` marker
            # param (MARKER_LOC) and the whole-body cases below - the validation
            # error shape stays consistent regardless of how the body is declared.
            raise RequestValidationError(
                [
                    {
                        "loc": ["body", *err["loc"]],
                        "msg": err["msg"],
                        "type": err["type"],
                    }
                    for err in e.errors()
                ]
            ) from e
        except ValidationError:
            raise
        except Exception as err:
            raise RequestValidationError(
                [
                    {
                        "loc": ["body"],
                        "msg": "Invalid request body",
                        "type": "value_error",
                    }
                ]
            ) from err

    async def _resolve_msgspec_body(self, slot: Any, request: Request) -> Any:
        # Reached only for a msgspec.Struct body slot, which the registration
        # tagging in `_handler_plan` produces only when msgspec is installed.
        import msgspec

        raw = await request.body()
        # An empty or whitespace-only body is "missing", not a decode error -
        # `msgspec.json.decode(b"")` would raise an opaque truncation error.
        if not raw or not raw.strip():
            if slot.is_optional:
                return None
            raise RequestValidationError(
                [{"loc": ["body"], "msg": "Missing request body", "type": "missing"}]
            )
        try:
            # `strict=True` (the default) enforces types on decode: a `str` for an
            # `int` field is rejected rather than coerced.
            return msgspec.json.decode(raw, type=slot.model)
        except msgspec.ValidationError as e:
            # `ValidationError` SUBCLASSES `DecodeError`, so it must be caught
            # first - reversing the order would let the `DecodeError` arm swallow
            # it. msgspec embeds the offending field path inside the message
            # text (e.g. "Expected `int`, got `str` - at `$.count`"); that format
            # is not a stable public API, so it is surfaced whole in `msg` rather
            # than parsed into `loc`, which stays `["body"]`.
            raise RequestValidationError(
                [{"loc": ["body"], "msg": str(e), "type": "value_error"}]
            ) from e
        except msgspec.DecodeError as e:
            raise RequestValidationError(
                [{"loc": ["body"], "msg": "Invalid JSON body", "type": "value_error"}]
            ) from e

    async def _resolve_marker(
        self, slot: Any, request: Request, path_params: dict[str, str]
    ) -> Any:
        marker = slot.marker
        mk = slot.marker_kind
        lookup = slot.lookup_name

        # List-typed query / header / cookie / form marker
        # (`tags: list[str] = Query(...)` / `Header(...)` / `Cookie(...)`
        # / `Form(...)`) - collect every repeated value, not just the
        # first.
        if mk in (0, 2, 3, 5) and get_origin(slot.target_type) in (list, set, tuple):
            inner_args = get_args(slot.target_type)
            inner = inner_args[0] if inner_args else str
            if mk == 5:  # MK_FORM
                values = (await request.form()).getlist(lookup)
            elif mk == 2:  # MK_HEADER
                values = request.headers.getlist(lookup.lower())
            elif mk == 3:  # MK_COOKIE
                values = request.cookies.getlist(lookup)
            else:  # MK_QUERY
                values = request.query_params.getlist(lookup)
            loc = MARKER_LOC[mk]
            if not values:
                if marker.has_default:
                    return marker.resolve_default()
                if slot.is_optional:
                    return None
                raise _err_missing_marker(loc, slot.name)
            # Per-element `marker.validate(...)` constraints (min_length, ge, regex,
            # ...) are intentionally NOT enforced on list-typed markers here; only
            # coercion runs. The codegen path matches. Enforcing them is a behavior
            # change tracked separately, not a behavior-preserving fix.
            return [v if inner is str else _coerce_value(v, inner, slot.name, loc) for v in values]

        if mk == 1:  # MK_PATH
            raw = path_params.get(lookup)
        elif mk == 2:  # MK_HEADER
            raw = request.headers.get(lookup.lower())
        elif mk == 3:  # MK_COOKIE
            raw = request.cookies.get(lookup)
        elif mk == 4:  # MK_BODY
            body = await request.json()
            # `Body(embed=True)` - the value lives under the param name
            # inside the JSON object, rather than being the whole body.
            # `embed` is a declared slot always set in `ParamBase.__init__`.
            raw = body.get(lookup) if marker.embed and isinstance(body, dict) else body
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

        raw = _coerce_scalar(raw, slot.target_type, slot.name, loc)

        try:
            return marker.validate(raw, slot.name)
        except ValueError as e:
            raise RequestValidationError(
                [{"loc": [loc, slot.name], "msg": str(e), "type": "value_error"}]
            ) from e

    async def _exec_depends(self, slot: Any, request: Request, path_params: dict[str, str]) -> Any:
        """Resolve a K_DEPENDS slot with caching and override support."""
        dep_callable = slot.dep_callable
        actual = self._overrides.get(dep_callable, dep_callable)

        # The scopes this Security() pushes onto the chain. Plain `Depends` has
        # no scopes so this is empty and every scope branch below is skipped.
        new_scopes: list[str] = slot.target_type if isinstance(slot.target_type, list) else []

        # Cache key. The common case keys purely on callable identity. A
        # scope-sensitive Security() dependency (its sub-graph reads
        # `SecurityScopes`) keys on the callable plus the scope union it will
        # see, so the same callable referenced with different scope sets in one
        # request resolves as distinct cached entries instead of colliding.
        # The bit is precomputed at registration, so the hot path branches once.
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
            # Override path: subplan + the three callable-kind flags are
            # memoised on the override callable; the resolver shares this
            # cache across requests via the Veloce instance.
            entry = self._override_subplans.get(actual)
            if entry is None:
                # local: avoids dependency <-> _handler_plan cycle
                from veloce._handler_plan import build_plan

                entry = (
                    build_plan(actual),
                    _is_async_callable(actual),
                    inspect.isgeneratorfunction(actual),
                    inspect.isasyncgenfunction(actual),
                )
                # Override targets that aren't weak-referenceable (some
                # C-level callables) silently skip caching - re-probing
                # is fine, leaking the entry is not.
                # Before the first write, replace the shared module-level
                # sentinel with a per-resolver `WeakKeyDictionary`. Without
                # this swap, a resolver constructed outside the dispatcher
                # (tests, direct callers - see `__init__` docstring) would
                # mutate the sentinel and silently accumulate plans
                # process-globally across unrelated callers.
                if self._override_subplans is _EMPTY_OVERRIDE_SUBPLANS:
                    self._override_subplans = weakref.WeakKeyDictionary()
                with contextlib.suppress(TypeError):
                    self._override_subplans[actual] = entry
            sub_plan, is_coro, is_gen, is_async_gen = entry

        # Push this Security()'s scopes onto the stack so a `SecurityScopes`
        # slot inside the sub-plan sees them. Plain `Depends` has no scopes
        # so the push is a no-op. The stack is popped after the sub-plan
        # resolves regardless of success/failure.
        if new_scopes:
            self._scope_stack.extend(new_scopes)
        try:
            sub_kwargs = await self._resolve_slots(sub_plan, request, path_params)
        finally:
            if new_scopes:
                del self._scope_stack[-len(new_scopes) :]

        if is_gen:
            # Sync generator: start it, capture the first yielded value as
            # the dependency result, and push the live generator onto the
            # teardown stack so `run_teardowns` advances past the yield.
            gen = actual(**sub_kwargs)
            try:
                result = next(gen)
            except StopIteration as err:
                raise RuntimeError(
                    f"yield dependency {actual!r} returned without yielding a value"
                ) from err
            self._teardowns.append(("sync", gen))
        elif is_async_gen:
            agen = actual(**sub_kwargs)
            try:
                result = await agen.__anext__()
            except StopAsyncIteration as err:
                raise RuntimeError(
                    f"yield dependency {actual!r} returned without yielding a value"
                ) from err
            self._teardowns.append(("async", agen))
        elif is_coro:
            result = await actual(**sub_kwargs)
        elif slot.dep_offload:
            # Opt-in: a blocking sync dependency runs in the thread pool so it
            # cannot stall the event loop; `offload` preserves request-scoped
            # ContextVars, the same pattern sync route handlers use.
            result = await offload(actual, **sub_kwargs)
        else:
            result = actual(**sub_kwargs)

        if slot.use_cache:
            self._cache[cache_key] = result
        return result
