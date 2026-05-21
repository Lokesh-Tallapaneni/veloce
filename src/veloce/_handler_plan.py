"""Pre-computed parameter-resolution plan for a route handler.

Built once at route registration; consumed per request. Removes the per-request
cost of `inspect.signature` and `typing.get_type_hints` from the dispatch path.

The plan is a list of `_Slot` records — one per handler parameter — each tagged
with the source the request should be read from. The plan also carries
pre-planned route-level dependencies and the recursive plans for any
`Depends` sub-graph reachable from the handler.

Reflection happens at registration time only, never on the hot path.
"""

from __future__ import annotations

import functools
import inspect
import types
from collections.abc import Callable
from typing import Any, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from veloce.background import BackgroundTasks
from veloce.http.datastructures import UploadFile
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.routing.params import Body, Cookie, File, Form, Header, Path, _ParamBase

# `Depends` is imported lazily inside builders to break the dependency.py ↔
# _handler_plan.py cycle: dependency.py imports the plan API at module load,
# and we are imported back via that chain.


# Slot kinds — bare integers for cheap dispatch in the hot loop.
K_REQUEST = 0
K_BG_TASKS = 1
K_DEPENDS = 2
K_PARAM_MARKER = 3
K_PATH = 4
K_QUERY = 5
K_QUERY_LIST = 6
K_BODY_MODEL = 7
K_UPLOAD_FILE = 8
K_DEFAULT = 9
K_NONE = 10
K_SECURITY_SCOPES = 11
K_RESPONSE = 12
K_WEBSOCKET = 13

# Marker kinds (for K_PARAM_MARKER slots).
MK_QUERY = 0
MK_PATH = 1
MK_HEADER = 2
MK_COOKIE = 3
MK_BODY = 4
MK_FORM = 5
MK_FILE = 6


def _unwrap_optional(annotation: Any) -> tuple[bool, Any]:
    """Detect `Optional[T]` / `Union[T, None]` / `T | None` and unwrap T."""
    origin = get_origin(annotation)
    # `Union[X, None]` has origin typing.Union; `X | None` (PEP 604) has origin
    # types.UnionType — both must be recognised.
    if origin is Union or origin is types.UnionType:
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and type(None) in args:
            return True, non_none[0]
    return False, annotation


def _unwrap_list(annotation: Any) -> tuple[bool, Any]:
    """Detect `list[T]` and unwrap T."""
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        return True, (args[0] if args else str)
    return False, annotation


def _marker_kind(marker: _ParamBase) -> int:
    if isinstance(marker, Path):
        return MK_PATH
    if isinstance(marker, Header):
        return MK_HEADER
    if isinstance(marker, Cookie):
        return MK_COOKIE
    if isinstance(marker, Body):
        return MK_BODY
    if isinstance(marker, Form):
        return MK_FORM
    if isinstance(marker, File):
        return MK_FILE
    # Query is the default and a bare _ParamBase falls here too.
    return MK_QUERY


class _Slot:
    """One handler parameter's pre-resolved binding to a request source.

    All fields packed onto one class — branching on `kind` is cheaper than
    dispatching on object type in the request hot path.
    """

    __slots__ = (
        "kind",
        "name",
        "target_type",
        "default",
        "has_default",
        "is_optional",
        "list_inner",
        "model",
        "marker",
        "marker_kind",
        "lookup_name",
        "sub_plan",
        "use_cache",
        "dep_callable",
        "dep_is_coro",
        "dep_is_gen",
        "dep_is_async_gen",
    )

    def __init__(self, kind: int, name: str) -> None:
        self.kind = kind
        self.name = name
        self.target_type: Any = None
        self.default: Any = None
        self.has_default = False
        self.is_optional = False
        self.list_inner: Any = None
        self.model: Any = None
        self.marker: _ParamBase | None = None
        self.marker_kind = MK_QUERY
        self.lookup_name = ""
        self.sub_plan: HandlerPlan | None = None
        self.use_cache = True
        self.dep_callable: Callable | None = None
        self.dep_is_coro = False
        # Yield-style dependencies (the typed-DI "context-manager" pattern): the
        # resolver starts the generator, captures the first yielded value as
        # the dependency result, and runs teardown after the response.
        self.dep_is_gen = False
        self.dep_is_async_gen = False


class HandlerPlan:
    """Frozen resolution plan for one handler, plus its dependency graph."""

    __slots__ = ("handler", "is_coro", "slots", "route_dep_plans")

    def __init__(
        self,
        handler: Callable,
        slots: list[_Slot],
        route_dep_plans: list[_Slot],
    ) -> None:
        self.handler = handler
        self.is_coro = inspect.iscoroutinefunction(handler)
        self.slots = slots
        # Each entry is a K_DEPENDS slot; only used for side-effect deps that
        # do not bind to a handler parameter.
        self.route_dep_plans = route_dep_plans


def _build_depends_slot(
    name: str, dep: Any, inferred: Any = None, *, websocket: bool = False
) -> _Slot:
    """Build a K_DEPENDS slot, recursively planning the sub-callable.

    `Depends()` with no explicit dependency falls back to `inferred`
    (the parameter's type annotation) — the shorthand for
    `x: SomeClass = Depends()`. `websocket` is threaded down the chain so
    a dependency of a WebSocket handler can itself receive the
    `WebSocket` connection by annotation or `ws` / `websocket` name.
    """
    slot = _Slot(K_DEPENDS, name)
    slot.use_cache = dep.use_cache
    callable_ = dep.dependency if dep.dependency is not None else inferred
    slot.dep_callable = callable_
    slot.dep_is_coro = inspect.iscoroutinefunction(callable_)
    slot.dep_is_gen = inspect.isgeneratorfunction(callable_)
    slot.dep_is_async_gen = inspect.isasyncgenfunction(callable_)
    slot.sub_plan = build_plan(callable_, websocket=websocket)
    # Security() scopes flow down the chain so a `SecurityScopes`
    # parameter anywhere below sees the union. Plain `Depends` has no
    # scopes attribute; we read defensively.
    scopes = getattr(dep, "scopes", None)
    if scopes:
        slot.target_type = list(scopes)  # repurpose target_type as scope list
    return slot


def build_plan(handler: Callable, *, websocket: bool = False) -> HandlerPlan:
    """Inspect `handler` and freeze a resolution plan.

    Called exactly once per route registration. Safe to call on builtins,
    lambdas, partials, or callable classes — returns an empty plan for
    handlers without inspectable signatures.

    When `websocket` is set, a parameter typed `WebSocket` (or named `ws`
    / `websocket`) is bound to a `K_WEBSOCKET` slot instead of being read
    from the request — the same plan machinery then drives WebSocket
    dependency injection, giving it `yield`-teardown and `Security` parity
    with the HTTP path.
    """
    from veloce.dependency import Depends  # local import breaks the import cycle

    ws_type: Any = None
    if websocket:
        from veloce.websocket import WebSocket

        ws_type = WebSocket

    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return HandlerPlan(handler, [], [])

    # `inspect.signature` transparently follows the class -> `__init__`,
    # callable-instance -> `__call__`, and `functools.partial` -> wrapped
    # function indirection, but `get_type_hints` does not — on a class it
    # returns the *class-level* annotations, not `__init__`'s. Resolve the
    # same object `signature` did so dependencies keep their parameter types.
    real: Any = handler
    while isinstance(real, functools.partial):
        real = real.func
    if inspect.isclass(real):
        hint_target: Any = real.__init__
    elif not inspect.isfunction(real) and not inspect.ismethod(real) and callable(real):
        hint_target = type(real).__call__
    else:
        hint_target = real

    try:
        # `include_extras=True` keeps PEP 593 `Annotated[T, Depends(...)]`
        # metadata in the result so we can detect dependency markers
        # without forcing users to use the default-value form.
        hints = get_type_hints(hint_target, include_extras=True)
    except Exception:
        # get_type_hints chokes on forward refs / private modules; degrade
        # gracefully — slots that need annotations become K_DEFAULT/K_NONE.
        hints = {}

    slots: list[_Slot] = []

    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue

        annotation = hints.get(param_name)
        default = param.default
        has_default = default is not inspect.Parameter.empty

        # PEP 593: `Annotated[T, Depends(...)]` or `Annotated[T, Query(...)]`.
        # If the metadata carries a `Depends` (or `_ParamBase` marker) and
        # the user didn't ALSO set it as the default, hoist the marker
        # into `default` and reduce `annotation` to the inner type.
        if get_origin(annotation) is not None and hasattr(annotation, "__metadata__"):
            meta_args = get_args(annotation)
            base_type = meta_args[0] if meta_args else annotation
            metadata = getattr(annotation, "__metadata__", ())
            extracted_marker: Any = None
            for m in metadata:
                if isinstance(m, (Depends, _ParamBase)):
                    extracted_marker = m
                    break
            if extracted_marker is not None and not isinstance(default, (Depends, _ParamBase)):
                default = extracted_marker
                has_default = True
            annotation = base_type

        # WebSocket injection (websocket plans only) — by annotation or by
        # the `ws` / `websocket` parameter name. Checked first so it wins
        # over the request/path fallbacks for a WebSocket handler.
        if websocket and (annotation is ws_type or param_name in ("ws", "websocket")):
            slots.append(_Slot(K_WEBSOCKET, param_name))
            continue

        # Request injection — either by name or by annotation.
        if param_name == "request" or annotation is Request:
            slots.append(_Slot(K_REQUEST, param_name))
            continue

        # BackgroundTasks injection by annotation.
        if annotation is BackgroundTasks:
            slots.append(_Slot(K_BG_TASKS, param_name))
            continue

        # Response injection by annotation. The handler
        # receives a fresh Response it can mutate (status_code, headers,
        # cookies); the dispatcher merges those onto the final response.
        if annotation is Response:
            slots.append(_Slot(K_RESPONSE, param_name))
            continue

        # SecurityScopes — receives the accumulated Security() chain scopes.
        # Lazy import avoids the dependency.py ↔ _handler_plan.py cycle.
        from veloce.dependency import SecurityScopes

        if annotation is SecurityScopes:
            slots.append(_Slot(K_SECURITY_SCOPES, param_name))
            continue

        # Depends() / Security() in default position. A bare `Depends()`
        # with no callable infers the dependency from the annotation
        # (`x: SomeClass = Depends()`).
        if isinstance(default, Depends):
            slots.append(
                _build_depends_slot(param_name, default, inferred=annotation, websocket=websocket)
            )
            continue

        # Explicit parameter markers (Query/Path/Header/Cookie/Body/Form/File).
        if isinstance(default, _ParamBase):
            slot = _Slot(K_PARAM_MARKER, param_name)
            slot.marker = default
            slot.marker_kind = _marker_kind(default)
            slot.lookup_name = default.alias or param_name
            # An un-aliased Header param converts `_` → `-`
            # in its name (`x_token` → `x-token`) unless disabled.
            if (
                slot.marker_kind == MK_HEADER
                and not default.alias
                and getattr(default, "convert_underscores", True)
            ):
                slot.lookup_name = slot.lookup_name.replace("_", "-")
            is_opt, inner = _unwrap_optional(annotation) if annotation else (False, annotation)
            slot.is_optional = is_opt
            slot.target_type = inner if is_opt else annotation
            slots.append(slot)
            continue

        is_optional, inner_type = (
            _unwrap_optional(annotation) if annotation else (False, annotation)
        )
        is_list, list_inner = _unwrap_list(inner_type) if inner_type else (False, inner_type)

        # UploadFile binding (with or without Optional).
        if annotation is UploadFile or (is_optional and inner_type is UploadFile):
            slot = _Slot(K_UPLOAD_FILE, param_name)
            slot.is_optional = is_optional
            slots.append(slot)
            continue

        # Pydantic model from body.
        if inner_type and isinstance(inner_type, type) and issubclass(inner_type, BaseModel):
            slot = _Slot(K_BODY_MODEL, param_name)
            slot.model = inner_type
            slot.is_optional = is_optional
            slots.append(slot)
            continue

        # List-typed parameter: read from query as a list.
        if is_list:
            slot = _Slot(K_QUERY_LIST, param_name)
            slot.list_inner = list_inner
            slot.has_default = has_default
            slot.default = default if has_default else None
            slot.is_optional = is_optional
            slots.append(slot)
            continue

        # Default fallback: path-or-query, decided at resolve time because
        # path_params are scope-local (per match). The slot is K_PATH-or-QUERY
        # ambiguous; we pick K_QUERY and the resolver will prefer path_params
        # when the name is present there. This keeps the plan handler-local
        # (one plan per handler, reusable across overrides).
        slot = _Slot(K_QUERY, param_name)
        slot.target_type = inner_type if inner_type is not None else str
        slot.is_optional = is_optional
        slot.has_default = has_default
        slot.default = default if has_default else None
        slots.append(slot)

    return HandlerPlan(handler, slots, [])


def build_route_dep_plans(route_dependencies: list, *, websocket: bool = False) -> list[_Slot]:
    """Pre-plan a route's `dependencies=[Depends(...), ...]` list."""
    from veloce.dependency import Depends  # local import breaks the cycle

    out: list[_Slot] = []
    for dep in route_dependencies:
        if isinstance(dep, Depends):
            out.append(_build_depends_slot("", dep, websocket=websocket))
    return out
