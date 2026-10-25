"""Dependency injection — pre-planned at registration, executed per request.

Public API: `Depends`, `Security`, `SecurityScopes`. The resolver walks a
pre-built `HandlerPlan` (see `veloce._handler_plan`) rather than reflecting on
the handler signature per request.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import functools
import inspect
import logging
import weakref
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast, get_args, get_origin

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from typing_extensions import Doc

from veloce._constants import (
    MSG_FIELD_REQUIRED,
    MSG_MISSING_PARAMETER,
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
    MK_COOKIE,
    MK_FORM,
    MK_HEADER,
    _fixed_shape_args,
    build_plan,
    build_route_dep_plans,
)
from veloce._internal import (
    _BaseExceptionGroup,
    _is_async_callable,
    _refuse_string_scopes,
    json_body_refused,
    offload,
)
from veloce._model_backend import ModelBackend, _msgspec, adapter_for, is_pydantic_model
from veloce._resolver_codegen import compile_graph_resolver, compile_param_resolver
from veloce.background import BackgroundTasks
from veloce.exceptions import HTTPException, RequestValidationError, ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.datastructures import Cookies, FormData, Headers, QueryParams
from veloce.http.request import Request
from veloce.http.response import Response

_logger = logging.getLogger(__name__)


# A rejected compiled-resolver build, so `resolve_plan` does not retry it.
_NOT_COMPILABLE = object()

# Shared sentinels; `_exec_depends` swaps the slot rather than mutating one.
_EMPTY_OVERRIDES: dict[Callable[..., Any], Callable[..., Any]] = {}
_EMPTY_OVERRIDE_SUBPLANS: weakref.WeakKeyDictionary[Callable[..., Any], Any] = (
    weakref.WeakKeyDictionary()
)


# ── Helpers ───────────────────────────────────────────────


@functools.lru_cache(maxsize=512)
def _type_adapter(target_type: Any) -> TypeAdapter[Any] | None:
    """Build (once) and cache a Pydantic ``TypeAdapter`` for a parameter type.

    ``None``, the result for an annotation Pydantic cannot adapt, is cached too.
    """
    try:
        return TypeAdapter(target_type)
    except Exception:
        return None


# What Pydantic accepts, so `?errors_only=ture` cannot mean "no filter".
_TRUE_VALUES = frozenset({"1", "on", "t", "true", "y", "yes"})
_FALSE_VALUES = frozenset({"0", "off", "f", "false", "n", "no"})


def _coerce_bool_value(value: str) -> bool:
    """Read a request string as a boolean, refusing anything that is not one."""
    lowered = value.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _coerce_literal(value: Any, target_type: Any, param_name: str, loc: str) -> Any:
    """Match a request value against a `Literal[...]` parameter.

    Request values arrive as strings, which Pydantic's strict literal check
    rejects for an `int` / `bool` literal. Candidates are compared by *exact*
    runtime type, sidestepping Python's loose `1 == True` equivalence.
    """
    members = get_args(target_type)
    candidates: list[Any] = [value]
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            candidates.append(_coerce_bool_value(value))
        with contextlib.suppress(ValueError):
            candidates.append(int(value))
        with contextlib.suppress(ValueError):
            candidates.append(float(value))
    for member in members:
        # Matched on the member's value, but the member itself is returned.
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

    Handles every annotation the fast scalar branches of `_coerce_value` do not
    (`datetime`, `UUID`, `Decimal`, constrained generics). An annotation Pydantic
    cannot adapt passes through unchanged.
    """
    try:
        adapter = _type_adapter(target_type)
    except TypeError:
        # Unhashable annotation - `lru_cache` cannot key it.
        return value
    if adapter is None:
        return value
    try:
        # A model-typed query param arrives as serialised JSON (`?tag={"a":1}`).
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
                return _coerce_bool_value(value)
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

        # `Literal[...]` needs string-aware member matching; the rest Pydantic.
    if get_origin(target_type) is Literal:
        return _coerce_literal(value, target_type, param_name, loc)
    return _coerce_via_pydantic(value, target_type, param_name, loc)


def _coerce_scalar(value: Any, target_type: Any, param_name: str, loc: str) -> Any:
    """Coerce `value` to `target_type` unless it is a string or already structured.

    The guard shared by the marker and bare-query resolution tails, in HTTP and
    in the MCP argument binder.
    """
    if target_type and target_type is not str and not isinstance(value, (dict, list)):
        return _coerce_value(value, target_type, param_name, loc)
    return value


def _err_body_validation(
    exc: PydanticValidationError, root: str = "body"
) -> RequestValidationError:
    """Rebuild a Pydantic validation failure as this framework's 422 shape.

    Pydantic's per-field path is appended to `root`, so every way of declaring a
    body reports the same `["body", ...]` location; a marker reading from
    elsewhere passes its own `MARKER_LOC` root.
    """
    return RequestValidationError(
        [
            {"loc": [root, *err["loc"]], "msg": err["msg"], "type": err["type"]}
            for err in exc.errors()
        ]
    )


def _err_missing_marker(loc: str, name: str) -> RequestValidationError:
    """Build the missing-required error for a parameter marker.

    Covers `Query`, `Header`, `Cookie` and `Form`, which share the
    `value_error.missing` shape. A bare `K_QUERY` slot deliberately uses the
    distinct `'missing'` / `MSG_FIELD_REQUIRED` contract instead.
    """
    return RequestValidationError(
        [
            {
                "loc": [loc, name],
                "msg": MSG_MISSING_PARAMETER.format(name=name),
                "type": "value_error.missing",
            }
        ]
    )


def _list_marker_checks(
    marker: Any,
) -> tuple[int | None, int | None, Callable[[Any, str], Any] | None] | None:
    """Return what a list-typed marker enforces, or `None` when it declares nothing.

    `min_length` / `max_length` bound the item count on a repeated parameter
    (published as `minItems` / `maxItems`), so the per-element check is the same
    marker with those two cleared. Not memoized: `ParamBase` hashes by identity
    and a folded `Field` builds a fresh marker per plan.
    """
    per_item = (
        marker.ge is not None
        or marker.le is not None
        or marker.gt is not None
        or marker.lt is not None
        or marker.multiple_of is not None
        or marker.regex is not None
    )
    if not per_item and marker.min_length is None and marker.max_length is None:
        return None
    item_validate = None
    if per_item:
        item_marker = copy.copy(marker)
        item_marker.min_length = None
        item_marker.max_length = None
        item_validate = item_marker.validate
    return (marker.min_length, marker.max_length, item_validate)


def _as_declared_sequence(annotation: Any, items: list[Any]) -> Any:
    """Build the sequence type the annotation declares from validated members.

    A `set[int]` annotation must not hand the handler the list its members were
    collected in, or it receives duplicates its own annotation forbids.
    """
    origin = get_origin(annotation)
    if origin is set:
        return set(items)
    if origin is frozenset:
        return frozenset(items)
    if origin is tuple:
        return tuple(items)
    return items


def _collect_sequence_marker(values: list[str], target_type: Any, name: str, loc: str) -> Any:
    """Coerce repeated source values onto a declared sequence type.

    Called by the interpreted resolver and bound into the compiled one, so both
    agree about a fixed-shape `tuple[int, str]` and about a `set[str]`.
    """
    positional = _fixed_shape_args(target_type)
    if positional is not None:
        if len(values) != len(positional):
            # Also covers `tuple[()]`, which declares no positions at all.
            raise RequestValidationError(
                [
                    {
                        "loc": [loc, name],
                        "msg": f"{name} must have exactly {len(positional)} values",
                        "type": "value_error",
                    }
                ]
            )
        collected = [
            value if declared is str else _coerce_value(value, declared, name, loc)
            for value, declared in zip(values, positional, strict=True)
        ]
    else:
        args = get_args(target_type)
        inner = args[0] if args else str
        collected = [
            value if inner is str else _coerce_value(value, inner, name, loc) for value in values
        ]
    return _as_declared_sequence(target_type, collected)


def _validate_list_marker(
    values: list[Any],
    checks: tuple[int | None, int | None, Callable[[Any, str], Any] | None],
    name: str,
    loc: str,
) -> None:
    """Enforce a list-typed marker's resolved bounds on the collected values.

    Takes what `_list_marker_checks` returned rather than the marker, so the
    compiled resolver binds the tuple at registration.
    """
    min_items, max_items, item_validate = checks
    if min_items is not None and len(values) < min_items:
        raise RequestValidationError(
            [
                {
                    "loc": [loc, name],
                    "msg": f"{name} must have at least {min_items} items",
                    "type": "value_error",
                }
            ]
        )
    if max_items is not None and len(values) > max_items:
        raise RequestValidationError(
            [
                {
                    "loc": [loc, name],
                    "msg": f"{name} must have at most {max_items} items",
                    "type": "value_error",
                }
            ]
        )
    if item_validate is None:
        return
    try:
        for value in values:
            item_validate(value, name)
    except ValueError as e:
        raise RequestValidationError(
            [{"loc": [loc, name], "msg": str(e), "type": "value_error"}]
        ) from e


def _validate_body_list(raw: list[Any], slot: Any) -> Any:
    """Validate a JSON array body against the sequence type it declares.

    The members arrive already typed by the JSON decode, so the declared type is
    a contract to check rather than a string to parse. Checked through the same
    adapter a bare body model would use, which is Pydantic's lax answer: `"1"`
    is accepted for an `int`, `1.5` is not.
    """
    adapter = slot.list_adapter
    if adapter is None:
        # The declared container itself, `tuple[int, str]` being heterogeneous.
        adapter = slot.list_adapter = _type_adapter(slot.target_type)
    if adapter is None:
        # An unadaptable element type leaves the members as decoded.
        return raw
    try:
        validated: Any = adapter.validate_python(raw)
    except PydanticValidationError as e:
        raise _err_body_validation(e) from e
    return validated


# ── Markers ───────────────────────────────────────────────


#: Empty when msgspec is absent, making the `except` a no-op not a NameError.
_msgspec_validation_error: tuple[type[BaseException], ...] = (
    (_msgspec.ValidationError,) if _msgspec is not None else ()
)


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

    if TYPE_CHECKING:  # pragma: no cover
        # A marker is written as a parameter's DEFAULT, which a checker refuses
        # as unassignable; the resolver replaces it before the body runs.
        def __new__(cls, *args: Any, **kwargs: Any) -> Any: ...

    def __init__(
        self,
        dependency: Annotated[
            Callable[..., Any] | None,
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
        # Defaults off so a trivial pure dependency keeps its inline call.
        self.offload = offload


class Security(Depends):
    """Dependency marker with OAuth2 scopes for OpenAPI emission."""

    __slots__ = ("scopes",)

    def __init__(
        self,
        dependency: Annotated[
            Callable[..., Any] | None,
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
        # A `str` is iterable, so `scopes="read"` would grant on `"r"`.
        if isinstance(scopes, (str, bytes)):
            _refuse_string_scopes(scopes, "Security(scopes=...)")
        super().__init__(dependency=dependency, use_cache=use_cache, offload=offload)
        self.scopes = scopes or []


class SecurityScopes:
    """Aggregated OAuth 2.0 scopes for the current Security() chain.

    A handler or sub-dependency declaring a parameter of this type receives the
    union of every `Security(..., scopes=[...])` between the route entry and
    this point in the graph. An authorising dependency checks
    `security_scopes.scopes` against what the bearer token carries and builds a
    `WWW-Authenticate: Bearer scope="<...>"` header when denying.

    Per RFC 6749 Sec. 3.3 the scope-string serialisation is space-separated.
    """

    __slots__ = ("scopes", "scope_str")

    def __init__(self, scopes: list[str] | None = None) -> None:
        self.scopes: list[str] = list(scopes) if scopes else []
        self.scope_str: str = " ".join(self.scopes)

    def __repr__(self) -> str:
        return f"SecurityScopes({self.scopes!r})"


# ── Resolver ──────────────────────────────────────────────


def _validated(marker: Any, value: Any, name: str, loc: str) -> Any:
    """Apply a source-less marker's bounds to a value the caller already found."""
    try:
        return marker.validate(value, name)
    except ValueError as e:
        raise RequestValidationError(
            [{"loc": [loc, name], "msg": str(e), "type": "value_error"}]
        ) from e


def _resolve_scalar_param(slot: Any, request: Request, path_params: dict[str, str]) -> Any:
    """Resolve a scalar path-or-query parameter to its coerced value.

    A path binding wins when the matched params include the name, then the
    query string. With no value, a default or optional yields that; otherwise
    the parameter is reported missing.
    """
    name = slot.name
    marker = slot.marker if slot.marker_validates else None
    if name in path_params:
        value = _coerce_value(path_params[name], slot.target_type or str, name, "path")
        return value if marker is None else _validated(marker, value, name, "path")
    # The alias names the query key; the path keys on the declared name.
    lookup = slot.lookup_name
    if lookup in request.query_params:
        # Under the key the schema published, not the parameter name.
        value = _coerce_value(
            request.query_params[lookup], slot.target_type or str, lookup, "query"
        )
        return value if marker is None else _validated(marker, value, lookup, "query")
    if slot.has_default:
        # A plain mutable default is wrapped in a copying factory at registration
        # (see `_guard_plain_mutable_default`); immutable defaults read inline.
        if slot.default_factory is not None:
            return slot.default_factory()
        return slot._static_default
    if slot.is_optional:
        return None
    raise RequestValidationError(
        [{"loc": ("query", lookup), "msg": MSG_FIELD_REQUIRED, "type": "missing"}]
    )


def _resolve_list_param(slot: Any, request: Request, path_params: dict[str, str]) -> Any:
    """Resolve a list-typed path-or-query parameter to a coerced list.

    A single path value becomes a one-element list; a query key yields every
    value the URL carried (`?tag=a&tag=b` -> ["a", "b"]). Falls back to the
    default / optional, else raises `missing`.
    """
    name = slot.name
    checks = slot.list_checks
    if name in path_params:
        values = [_coerce_value(path_params[name], slot.list_inner, name, "path")]
        if checks is not None:
            _validate_list_marker(values, checks, name, "path")
        return values
    lookup = slot.lookup_name
    if lookup in request.query_params:
        values = [
            _coerce_value(v, slot.list_inner, lookup, "query")
            for v in request.query_params.getall(lookup)
        ]
        if checks is not None:
            _validate_list_marker(values, checks, lookup, "query")
        return values
    if slot.has_default:
        # A plain mutable default is wrapped in a copying factory at registration
        # (see `_guard_plain_mutable_default`); immutable defaults read inline.
        if slot.default_factory is not None:
            return slot.default_factory()
        return slot._static_default
    if slot.is_optional:
        return None
    raise RequestValidationError(
        [{"loc": ("query", lookup), "msg": MSG_FIELD_REQUIRED, "type": "missing"}]
    )


def _every_value(source: Any, key: str) -> list[str]:
    """Every value stored under `key`, from a multi-dict or a plain mapping.

    A `Request` carries multi-value headers and cookies; a `WebSocket` carries
    plain dicts built from the handshake, with no `.getlist`.
    """
    getlist = getattr(source, "getlist", None)
    if getlist is not None:
        values: list[str] = getlist(key)
        return values
    value = source.get(key)
    return [] if value is None else [value]


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
        """Fast path - consume a pre-built `HandlerPlan`."""
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

    async def _resolve_upload_file(
        self, slot: Any, request: Request, kwargs: dict[str, Any]
    ) -> None:
        """Bind an uploaded file from the multipart form, if present.

        Leaves the kwarg unset when the field is absent and not optional, so the
        handler's own default applies.
        """
        upload = (await request.form()).get(slot.name)
        if upload is not None:
            kwargs[slot.name] = upload
        elif slot.is_optional:
            kwargs[slot.name] = None

    async def _resolve_model_group(self, slot: Any, request: Request) -> Any:
        """Bind a model whose fields come from one request source.

        `Annotated[Filters, Query()]` reads every field of `Filters` off the
        query string rather than one key named `filters`. Errors are remapped
        onto `["query", "<field>"]`, so a bad `limit` blames `limit`.
        """
        mk = slot.marker_kind
        # Four branches, four mappings; only `getlist` and `get` are read.
        source: FormData | Headers | Cookies | QueryParams
        if mk == MK_FORM:
            source = await request.form()
        elif mk == MK_HEADER:
            source = request.headers
        elif mk == MK_COOKIE:
            source = request.cookies
        else:
            source = request.query_params

        getlist = getattr(source, "getlist", None)
        raw: dict[str, Any] = {}
        for validate_key, key, is_list in slot.group_fields:
            if is_list and getlist is not None:
                values = getlist(key.lower() if mk == MK_HEADER else key)
                if values:
                    raw[validate_key] = values
                continue
            value = source.get(key.lower() if mk == MK_HEADER else key)
            if value is not None:
                raw[validate_key] = value

        loc = MARKER_LOC[mk]
        if not raw and (slot.is_optional or slot.marker.has_default):
            # Fall back rather than reporting every field as missing.
            return slot.marker.resolve_default() if slot.marker.has_default else None
        try:
            if slot.backend == ModelBackend.MSGSPEC:
                return _msgspec.convert(raw, type=slot.model, strict=False)
            if slot.backend == ModelBackend.ADAPTED:
                return adapter_for(slot.model).validate_python(raw)
            return slot.model.model_validate(raw)
        except PydanticValidationError as e:
            raise _err_body_validation(e, loc) from e
        except Exception as err:
            raise RequestValidationError(
                [{"loc": [loc], "msg": str(err), "type": "value_error"}]
            ) from err

    async def _resolve_body_model(self, slot: Any, request: Request) -> Any:
        if slot.backend == ModelBackend.MSGSPEC:
            return await self._resolve_msgspec_body(slot, request)
        if slot.backend == ModelBackend.ADAPTED:
            return await self._resolve_adapted_body(slot, request)
        return await self._resolve_pydantic_body(slot, request)

    @staticmethod
    def _require_json_body(request: Request) -> None:
        """Refuse a body whose `Content-Type` declares it is not JSON.

        Which content types are refused lives in `json_body_refused`, so this
        door and `await request.json()` cannot answer differently. What this adds
        is the carve-out for an absent *body*: nothing was there to be sent
        cross-origin, so an omitted optional body is answered "absent" below.
        """
        mimetype = request.mimetype
        if not json_body_refused(mimetype):
            return
        if request._body_drained and not request._body.strip():
            return
        raise RequestValidationError(
            [
                {
                    "loc": ["body"],
                    "msg": f"Expected a JSON body; got Content-Type {mimetype!r}",
                    "type": "value_error",
                }
            ]
        )

    async def _resolve_adapted_body(self, slot: Any, request: Request) -> Any:
        """Validate a dataclass / `TypedDict` body through its cached adapter."""
        self._require_json_body(request)
        raw = await request.body()
        if not raw.strip():
            if slot.is_optional:
                return None
            raise RequestValidationError(
                [{"loc": ["body"], "msg": MSG_FIELD_REQUIRED, "type": "missing"}]
            )
        try:
            return adapter_for(slot.model).validate_json(raw)
        except PydanticValidationError as e:
            raise _err_body_validation(e) from e

    async def _resolve_pydantic_body(self, slot: Any, request: Request) -> Any:
        self._require_json_body(request)
        try:
            body_data = await request.json()
            return slot.model.model_validate(body_data)
        except PydanticValidationError as e:
            raise _err_body_validation(e) from e
        except ValidationError:
            raise
        except HTTPException:
            # `on_json_loading_failed` already raised `BadRequest` with a stable
            # message, the decoder's offsets deriving from attacker-controlled input.
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
        # Only for a msgspec.Struct slot, tagged only when msgspec is installed.
        self._require_json_body(request)
        raw = await request.body()
        # "missing", not the opaque truncation error `decode(b"")` would raise.
        if not raw or not raw.strip():
            if slot.is_optional:
                return None
            raise RequestValidationError(
                [{"loc": ["body"], "msg": "Missing request body", "type": "missing"}]
            )
        try:
            # `strict=True` rejects a `str` for an `int` rather than coercing.
            return _msgspec.json.decode(raw, type=slot.model)
        except _msgspec.ValidationError as e:
            # `ValidationError` SUBCLASSES `DecodeError`, so it is caught first; its
            # field path is in the message text, so `loc` stays `["body"]`.
            raise RequestValidationError(
                [{"loc": ["body"], "msg": str(e), "type": "value_error"}]
            ) from e
        except _msgspec.DecodeError as e:
            raise RequestValidationError(
                [{"loc": ["body"], "msg": "Invalid JSON body", "type": "value_error"}]
            ) from e

    @staticmethod
    def _validate_marker_body(value: Any, slot: Any) -> Any:
        """Validate an already-extracted `Body()` value against the slot's model.

        `payload: Payload = Body()` declares the same contract as a bare
        `payload: Payload`, down to the `["body", ...]` error location. It cannot
        reuse `_resolve_body_model`, which reads the whole body, because
        `embed=True` takes the value from under the parameter name.
        """
        backend = slot.backend
        try:
            if backend == ModelBackend.MSGSPEC:
                return _msgspec.convert(value, type=slot.model, strict=False)
            if backend == ModelBackend.ADAPTED:
                return adapter_for(slot.model).validate_python(value)
            return slot.model.model_validate(value)
        except PydanticValidationError as e:
            raise _err_body_validation(e) from e
        except _msgspec_validation_error as e:
            # The field path is in the message text, so `loc` stays `["body"]`.
            raise RequestValidationError(
                [{"loc": ["body"], "msg": str(e), "type": "value_error"}]
            ) from e

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
            # `Body(embed=True)`: under the param name, not the whole body.
            raw = body.get(lookup) if marker.embed and isinstance(body, dict) else body
            # `marker.validate` bounds a scalar, so an array is checked here.
            if slot.marker_reads_list and isinstance(raw, list):
                # The element type first, or a raw `["0"]` skips `items.minimum`.
                items = _validate_body_list(raw, slot)
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
