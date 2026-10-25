"""Handler plan — pre-computed parameter-resolution plan for a route handler.

A list of `_Slot` records - one per handler parameter - each tagged with the
source the request should be read from, plus the route-level dependencies and
the recursive plans for any reachable `Depends` sub-graph. Built once at route
registration, so reflection never happens on the hot path.
"""

from __future__ import annotations

import builtins
import contextlib
import copy
import inspect
import types
import typing
import warnings
import weakref
from collections.abc import Callable, Mapping
from typing import Any, Union, cast, get_args, get_origin, get_type_hints

import typing_extensions

from veloce._internal import _is_async_callable, resolve_hint_target
from veloce._model_backend import ModelBackend, _msgspec, backend_of
from veloce._params import (
    Body,
    Cookie,
    File,
    Form,
    Header,
    ParamBase,
    Path,
    deepcopyable,
    field_info_declares_constraints,
    field_info_uncarried_constraints,
    find_field_info,
    is_immutable,
    needs_per_request_copy,
)
from veloce._warnings import _RegistrationWarning
from veloce.background import BackgroundTasks
from veloce.http.datastructures import UploadFile
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.websocket import WebSocket

# `Depends` is imported lazily inside builders to break the dependency.py cycle.


# ── Slot-kind constants ───────────────────────────────────
# Bare integers (not IntEnum) for cheap branching in the request hot loop.
K_REQUEST = 0
K_BG_TASKS = 1
K_DEPENDS = 2
K_PARAM_MARKER = 3
K_QUERY = 5
K_QUERY_LIST = 6
K_BODY_MODEL = 7
K_UPLOAD_FILE = 8
K_SECURITY_SCOPES = 11
K_RESPONSE = 12
K_WEBSOCKET = 13
# A model whose FIELDS are read from one request source rather than the body.
K_MODEL_GROUP = 14

MK_QUERY = 0
MK_PATH = 1
MK_HEADER = 2
MK_COOKIE = 3
MK_BODY = 4
MK_FORM = 5
MK_FILE = 6

#: Every sequence origin a repeated value or a JSON array validates into, so the
#: body validator, the field spec and the schema lowering cannot list different sets.
_SEQUENCE_TYPES = (list, set, frozenset, tuple)
_SEQUENCE_ORIGINS = frozenset(_SEQUENCE_TYPES)


def _normalise_sequence_default(marker: ParamBase, target_type: Any) -> ParamBase:
    """Return a marker whose default is the container the annotation declares.

    `t: set[str] = Query(default=["a", "b"])` names a set and offers a list. A
    copy, never the marker itself: one marker can be shared between routes.
    Whether the result is shared per request or rebuilt is decided by what is in
    it - a mutable or mutably-held member means a fresh value each time. An
    unconvertible default is left as written rather than raising.
    """
    origin = get_origin(target_type)
    if origin not in _SEQUENCE_ORIGINS or not marker.has_default:
        return marker
    source = marker.default
    if not isinstance(source, _SEQUENCE_TYPES):
        if marker.default_factory is None:
            return marker
        inner_factory = marker.default_factory
        normalised = copy.copy(marker)
        normalised.default_factory = lambda: _rebuild_sequence(origin, inner_factory())
        return normalised
    # Read from the static default even when a factory exists, which would copy
    # a long list only to rebuild it as a set.
    members = tuple(source)
    try:
        built = origin(members)
    except TypeError:
        return marker
    normalised = copy.copy(marker)
    if not is_immutable(members) and deepcopyable(members):
        normalised.default_factory = lambda: origin(copy.deepcopy(members))
    elif origin is set or origin is list:
        # Copying the built container, not rebuilding: a set copy re-uses its hashes.
        normalised.default_factory = cast("set[Any] | list[Any]", built).copy
    else:
        normalised.default = built
        normalised.default_factory = None
    return normalised


def _rebuild_sequence(origin: Any, value: Any) -> Any:
    """Build `origin` from `value`, leaving it alone if it cannot be built."""
    if not isinstance(value, _SEQUENCE_TYPES):
        # `set` accepts anything iterable: `"ab"` becomes `{"a", "b"}`.
        return value
    try:
        return origin(value)
    except TypeError:
        # The author's bug, but this runs per request: raising would 500 each one.
        return value


def _fixed_shape_args(annotation: Any) -> tuple[Any, ...] | None:
    """Return a fixed-shape tuple's per-position types, or `None` if homogeneous.

    `tuple[int, str]` declares two positions; `tuple[int, ...]`, `list[int]` and
    `set[int]` declare one element type for every member.
    """
    if get_origin(annotation) is not tuple:
        return None
    args = get_args(annotation)
    if Ellipsis in args:
        return None
    if args == ((),):
        # How `tuple[()]` decomposes before 3.11: zero positions, not "no element
        # type".
        return ()
    return args


#: Single-sourced so the interpreter and the codegen read one map.
MARKER_LOC = {
    MK_QUERY: "query",
    MK_PATH: "path",
    MK_HEADER: "header",
    MK_COOKIE: "cookie",
    MK_BODY: "body",
    MK_FORM: "form",
    MK_FILE: "form",
}


# ── Annotation helpers ────────────────────────────────────
def _unwrap_optional(annotation: Any) -> tuple[bool, Any]:
    """Detect `Optional[T]` / `Union[T, None]` / `T | None` and unwrap T."""
    origin = get_origin(annotation)
    # `Union[X, None]` has origin typing.Union, `X | None` types.UnionType.
    if origin is Union or origin is types.UnionType:
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and type(None) in args:
            return True, non_none[0]
    return False, annotation


def extract_annotated_marker(annotation: Any) -> tuple[Any, Any]:
    """Split `Annotated[T, Depends()/Query()/...]` into `(marker, T)`.

    `(None, annotation)` when there is no Veloce marker. The one marker
    resolver, shared with the OpenAPI walkers, so what runs and what is
    published cannot disagree. Python 3.10's `get_type_hints` still applies the
    implicit-Optional rule PEP 484 dropped in 3.11, wrapping `Annotated` in
    `Optional[...]` and hiding the marker; that wrapper is peeled here.
    """
    if annotation is not None and not hasattr(annotation, "__metadata__"):
        was_optional, inner = _unwrap_optional(annotation)
        if was_optional and hasattr(inner, "__metadata__"):
            annotation = inner

    if get_origin(annotation) is None or not hasattr(annotation, "__metadata__"):
        return None, annotation

    from veloce.dependency import Depends  # local import breaks the import cycle

    meta_args = get_args(annotation)
    base_type = meta_args[0] if meta_args else annotation
    for meta in getattr(annotation, "__metadata__", ()):
        if isinstance(meta, (Depends, ParamBase)):
            return meta, base_type
    return None, base_type


def _annotated_field_info(annotation: Any) -> Any:
    """Return the pydantic `FieldInfo` an `Annotated` parameter carries, or `None`.

    Python 3.10's implicit-`Optional` wrapper is peeled first, as
    `extract_annotated_marker` peels it.
    """
    if annotation is not None and not hasattr(annotation, "__metadata__"):
        was_optional, inner = _unwrap_optional(annotation)
        if was_optional:
            annotation = inner
    return find_field_info(annotation)


def _unwrap_list(annotation: Any) -> tuple[bool, Any]:
    """Detect `list[T]` and unwrap T."""
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        return True, (args[0] if args else str)
    return False, annotation


def _group_field_specs(
    model: Any, backend: ModelBackend, mk: int
) -> tuple[tuple[str, str, bool], ...]:
    """Resolve a grouped model's fields to `(validate_key, lookup_key, is_list)`.

    Two names because they differ: the value is read from the source under
    `lookup_key` (an alias, or a `Header` field's hyphenated spelling) and
    validated under `validate_key`. Decided here, so the request path is a
    plain `dict` build.
    """
    specs: list[tuple[str, str, bool]] = []
    if backend is ModelBackend.PYDANTIC:
        items = [
            (name, getattr(f, "alias", None), getattr(f, "annotation", None))
            for name, f in model.model_fields.items()
        ]
    elif backend is ModelBackend.ADAPTED:
        # A dataclass or TypedDict declares no aliases, so the wire name is the name.
        items = [(name, None, tp) for name, tp in get_type_hints(model).items()]
    else:
        items = [(f.name, f.encode_name, f.type) for f in _msgspec.structs.fields(model)]
    for name, alias, annotation in items:
        validate_key = alias or name
        key = validate_key
        # As a scalar `Header()` does: `x_token` spells `x-token` on the wire.
        if mk == MK_HEADER and not alias:
            key = key.replace("_", "-")
        origin = get_origin(annotation)
        specs.append((validate_key, key, origin in _SEQUENCE_ORIGINS))
    return tuple(specs)


def _marker_kind(marker: ParamBase) -> int:
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
    # Query is the default and a bare ParamBase falls here too.
    return MK_QUERY


def _raise_kwarg_ambiguity(
    handler: Callable[..., Any], param_name: str, marker: Any, seen: list[Any] | None
) -> None:
    """Raise on a by-name magic parameter that also carries a value marker."""
    # local: breaks the exceptions <-> http.response cycle (isort would reorder it)
    from veloce.exceptions import ConfigurationError

    marker_name = type(marker).__name__
    chain = ""
    # A top-level handler already names itself below.
    if seen and len(seen) > 1:
        names = [_callable_name(c) for c in seen]
        chain = f" (in dependency chain {' -> '.join(names)})"
    where = _callable_name(handler)
    raise ConfigurationError(
        f"parameter {param_name!r} on {where}{chain} is reserved for the injected "
        f"{param_name!r} object but also declares a {marker_name}() marker; the marker "
        f"would be silently ignored. Rename the parameter or drop the {marker_name}() "
        "marker to resolve the ambiguity."
    )


def _warn_shared_mutable_default(name: str, default: Any, *, stacklevel: int = 3) -> None:
    """Warn (once at registration) about a marker's shared mutable default.

    `stacklevel` counts the frames between here and the registration call the
    author wrote.
    """
    kind = type(default).__name__
    # Such a container is shared through the member, and `tuple` is not the fix.
    what = (
        f"a {kind} default holding a mutable member"
        if isinstance(default, (tuple, frozenset))
        else f"a mutable {kind} default"
    )
    fix = (
        "default_factory" if isinstance(default, (tuple, frozenset)) else f"default_factory={kind}"
    )
    warnings.warn(
        f"parameter {name!r} has {what} that is shared across requests; "
        f"pass {fix} so each request gets its own value",
        _RegistrationWarning,
        stacklevel=stacklevel,
    )


def _owned(name: str, owner: str) -> str:
    """Name a parameter, and the handler it belongs to when the caller knows."""
    return f"{owner}: parameter {name!r}" if owner else f"parameter {name!r}"


def _warn_field_info_overridden(
    name: str, marker: Any, owner: str = "", *, stacklevel: int = 3
) -> None:
    """Warn that a parameter's `Field` lost to the marker declared beside it.

    Declaring both says two things about one value, and the marker is the one
    Veloce acts on.
    """
    from veloce.dependency import Depends  # local import breaks the import cycle

    kind = type(marker).__name__
    # A `Depends()` / `Security()` carries no constraints, so it is not advice.
    remedy = "" if isinstance(marker, Depends) else f" - declare them on the {kind}() instead"
    warnings.warn(
        f"{_owned(name, owner)} declares both a pydantic Field() and a "
        f"{kind}() marker; the marker wins and the Field's "
        f"constraints are ignored{remedy}",
        _RegistrationWarning,
        stacklevel=stacklevel,
    )


def _warn_field_constraint_uncarried(name: str, keywords: tuple[str, ...], owner: str = "") -> None:
    """Warn that a `Field` declares a bound no parameter marker can carry.

    pydantic keeps `max_digits` / `decimal_places` / `allow_inf_nan` on its own
    metadata object, so the fold cannot see them to carry.
    """
    warnings.warn(
        f"{_owned(name, owner)} declares {', '.join(keywords)} on its pydantic Field(), which a "
        "parameter marker cannot carry, so it is neither enforced nor published; validate it "
        "inside the handler or declare the parameter with a model",
        _RegistrationWarning,
        stacklevel=4,
    )


def _warn_field_info_unused(
    name: str, binding: str, owner: str = "", *, stacklevel: int = 4
) -> None:
    """Warn that a `Field` cannot be carried by the binding its annotation chose.

    A body is validated by the model or read as an upload, neither of which
    consults a parameter marker.
    """
    warnings.warn(
        f"{_owned(name, owner)} carries a pydantic Field(), but its annotation binds it to "
        f"{binding}, where a parameter marker's constraints have no effect; they are "
        "ignored",
        _RegistrationWarning,
        stacklevel=stacklevel,
    )


def _guard_plain_mutable_default(slot: _Slot, name: str, *, stacklevel: int = 3) -> None:
    """Stop a plain `param: list = []` default from being shared across requests.

    A bare mutable default is one object on the signature, so returning it by
    identity lets one request's in-place mutation leak into the next.
    """
    if slot.has_default and needs_per_request_copy(slot._static_default):
        original = slot._static_default
        if deepcopyable(original):
            slot.default_factory = lambda: copy.deepcopy(original)
        _warn_shared_mutable_default(name, original, stacklevel=stacklevel)


# ── Slot record ───────────────────────────────────────────
class _Slot:
    """One handler parameter's pre-resolved binding to a request source.

    All fields packed onto one class - branching on `kind` is cheaper than
    dispatching on object type in the request hot path.
    """

    __slots__ = (
        "kind",
        "name",
        "target_type",
        "_static_default",
        "default_factory",
        "has_default",
        "is_optional",
        "list_inner",
        "list_adapter",
        "model",
        "marker",
        "marker_kind",
        "lookup_name",
        "group_fields",
        "sub_plan",
        "use_cache",
        "dep_callable",
        "dep_is_coro",
        "dep_is_gen",
        "dep_is_async_gen",
        "dep_offload",
        "scope_sensitive",
        "backend",
        "list_checks",
        "marker_reads_list",
        "marker_validates",
    )

    def __init__(self, kind: int, name: str) -> None:
        self.kind = kind
        self.name = name
        self.target_type: Any = None
        self._static_default: Any = None
        # Set only for slots carrying one; the rest read the static `default`.
        self.default_factory: Callable[[], Any] | None = None
        self.has_default = False
        self.is_optional = False
        self.list_inner: Any = None
        # Memoised on first use: the `list[T]` alias and its adapter lookup cost
        # ~230ns, a quarter of the validation, and never change for the route.
        self.list_adapter: Any = None
        self.model: Any = None
        self.marker: ParamBase | None = None
        self.marker_kind = MK_QUERY
        self.lookup_name = ""
        # K_MODEL_GROUP only, so the request path does no introspection.
        self.group_fields: tuple[tuple[str, str, bool], ...] = ()
        self.sub_plan: HandlerPlan | None = None
        self.use_cache = True
        self.dep_callable: Callable[..., Any] | None = None
        self.dep_is_coro = False
        self.dep_is_gen = False
        self.dep_is_async_gen = False
        self.dep_offload = False
        # Set when the sub-graph reads them, so the cache keys on the scopes too.
        self.scope_sensitive = False
        self.backend = ModelBackend.NONE
        # Resolved once, so no `_list_marker_checks` call per request.
        self.list_checks: Any = None
        # So the body branch does not run `get_origin` on a fixed annotation.
        self.marker_reads_list = False
        # A documentation-only `Field(description=...)` still lowers, via `marker`.
        self.marker_validates = False

    @property
    def default(self) -> Any:
        """This slot's default value, fresh per call when it needs to be.

        The shared object stays reachable as `_static_default` deliberately.
        Code generation MUST keep branching on `default_factory` and reading the
        raw field: it snapshots the value into the generated namespace once, and
        a property read there would bake in a single materialised copy.
        """
        factory = self.default_factory
        return factory() if factory is not None else self._static_default


# ── Parallel-dependency grouping ──────────────────────────
def _slot_parallel_safe(slot: _Slot, seen_plans: set[int]) -> bool:
    """Whether a K_DEPENDS slot and its whole sub-graph are parallel-safe.

    Unsafe when the slot (or any below it) pushes Security() scopes or is a
    yield-style dependency - both touch shared resolver state whose ordering
    parallel execution would corrupt. Cycle-guarded via `seen_plans`.
    """
    if isinstance(slot.target_type, list) and slot.target_type:
        return False
    if slot.dep_is_gen or slot.dep_is_async_gen:
        return False
    sub_plan = slot.sub_plan
    if sub_plan is None:
        return True
    plan_id = id(sub_plan)
    if plan_id in seen_plans:
        return True
    seen_plans.add(plan_id)
    for sub in sub_plan.slots:
        if sub.kind == K_DEPENDS and not _slot_parallel_safe(sub, seen_plans):
            return False
    return True


def _cached_callables(slot: _Slot, seen_plans: set[int]) -> set[Any]:
    """Every `use_cache=True` callable reachable from a K_DEPENDS subtree.

    Two dependencies sharing a cached callable must not run in the same wave:
    the first to resolve writes the shared result cache and the second reads it.
    The full reachable set, not only the top-level callable, catches the nested
    case. Cycle-guarded via `seen_plans`.
    """
    found: set[Any] = set()
    if slot.use_cache and slot.dep_callable is not None:
        found.add(slot.dep_callable)
    sub_plan = slot.sub_plan
    if sub_plan is None:
        return found
    plan_id = id(sub_plan)
    if plan_id in seen_plans:
        return found
    seen_plans.add(plan_id)
    for sub in sub_plan.slots:
        if sub.kind == K_DEPENDS:
            found |= _cached_callables(sub, seen_plans)
    return found


def compute_dep_waves(slots: list[_Slot]) -> list[list[int]]:
    """Topological waves of parallel-safe K_DEPENDS slot indices.

    Batches regardless of declaration order or intervening non-dependency slots;
    two dependencies sharing a cached callable land in successive waves, so the
    cache is filled once and never raced. Dependencies that are not
    parallel-safe are excluded entirely and stay inline in slot order. `[]` when
    fewer than two safe dependencies exist, so a linear chain adds no bookkeeping.
    """
    safe: list[int] = []
    cached_sets: dict[int, set[Any]] = {}
    for i, slot in enumerate(slots):
        if slot.kind != K_DEPENDS or not _slot_parallel_safe(slot, set()):
            continue
        safe.append(i)
        cached_sets[i] = _cached_callables(slot, set())

    if len(safe) < 2:
        return []

    waves: list[list[int]] = []
    wave_cached: list[set[Any]] = []
    for idx in safe:
        own_cached = cached_sets[idx]
        placed = False
        # Earliest wave sharing no cached callable; in order, so it stays stable.
        for wave, taken in zip(waves, wave_cached, strict=True):
            if own_cached.isdisjoint(taken):
                wave.append(idx)
                taken |= own_cached
                placed = True
                break
        if not placed:
            waves.append([idx])
            wave_cached.append(set(own_cached))

    # One slot per wave runs nothing concurrently; drop it for the plain path.
    if all(len(wave) < 2 for wave in waves):
        return []
    return waves


# ── Handler plan ──────────────────────────────────────────
class HandlerPlan:
    """Frozen resolution plan for one handler, plus its dependency graph."""

    __slots__ = (
        "handler",
        "is_coro",
        "slots",
        "route_dep_plans",
        "compiled_resolver",
        "compiled_graph_resolver",
        "dep_waves",
        "wave_trigger",
        "wave_members",
    )

    def __init__(
        self,
        handler: Callable[..., Any],
        slots: list[_Slot],
        route_dep_plans: list[_Slot],
    ) -> None:
        self.handler = handler
        self.is_coro = _is_async_callable(handler)
        self.slots = slots
        # K_DEPENDS slots for side-effect deps that bind to no parameter.
        self.route_dep_plans = route_dep_plans
        # Straight-line resolver for param-only plans. `None` = not yet attempted;
        # a callable = compiled fast path; a sentinel = tried and not compilable.
        self.compiled_resolver: Any = None
        # The same tri-state, for a no-wave dependency graph.
        self.compiled_graph_resolver: Any = None
        # Derived once so the resolver does not re-scan slot safety per request.
        self.dep_waves = compute_dep_waves(slots)
        # Every batch resolves at the earliest batched index, so running the
        # waves in order there fills a cache prerequisite first.
        members: set[int] = set()
        batched_waves: list[list[int]] = []
        for wave in self.dep_waves:
            if len(wave) < 2:
                continue
            batched_waves.append(sorted(wave))
            members.update(wave)
        trigger: dict[int, list[list[int]]] = {}
        if members:
            trigger[min(members)] = batched_waves
        self.wave_trigger = trigger
        self.wave_members = members


# ── Plan builders ─────────────────────────────────────────
#: Means "this route declares no path parameter", which only a caller knows.
_NO_PATH_PARAMS: frozenset[str] = frozenset()


def _build_depends_slot(
    name: str,
    dep: Any,
    inferred: Any = None,
    *,
    websocket: bool = False,
    path_params: frozenset[str] = _NO_PATH_PARAMS,
    _seen: list[Any] | None = None,
) -> _Slot:
    """Build a K_DEPENDS slot, recursively planning the sub-callable.

    `Depends()` with no explicit dependency falls back to `inferred`, the
    parameter's type annotation. `websocket` is threaded down the chain so a
    dependency of a WebSocket handler can itself receive the connection.
    """
    slot = _Slot(K_DEPENDS, name)
    slot.use_cache = dep.use_cache
    callable_ = dep.dependency if dep.dependency is not None else inferred
    slot.dep_callable = callable_
    slot.dep_is_coro = _is_async_callable(callable_)
    slot.dep_is_gen = inspect.isgeneratorfunction(callable_)
    slot.dep_is_async_gen = inspect.isasyncgenfunction(callable_)
    # Meaningful only for a plain sync callable; the rest have their own model.
    slot.dep_offload = bool(getattr(dep, "offload", False)) and not (
        slot.dep_is_coro or slot.dep_is_gen or slot.dep_is_async_gen
    )
    slot.sub_plan = build_plan(callable_, websocket=websocket, path_params=path_params, _seen=_seen)
    # Scopes flow down so a `SecurityScopes` parameter below sees the union.
    scopes = getattr(dep, "scopes", None)
    if scopes:
        # target_type is repurposed as the scope list for Security() slots.
        slot.target_type = list(scopes)
    # The cached result varies with the active scope union, so without this bit
    # one callable collides with itself.
    slot.scope_sensitive = _subgraph_reads_scopes(slot.sub_plan, set())
    return slot


def _subgraph_reads_scopes(plan: HandlerPlan | None, seen_plans: set[int]) -> bool:
    """Whether `plan` (or any dependency below it) reads `SecurityScopes`.

    Such a dependency's result depends on the active scope union, so its cache
    entry must be scope-keyed. Cycle-guarded via `seen_plans`.
    """
    if plan is None:
        return False
    plan_id = id(plan)
    if plan_id in seen_plans:
        return False
    seen_plans.add(plan_id)
    for sub in plan.slots:
        if sub.kind == K_SECURITY_SCOPES:
            return True
        if sub.kind == K_DEPENDS and _subgraph_reads_scopes(sub.sub_plan, seen_plans):
            return True
    return False


#: Memoised per handler; a caller mutating `__annotations__` needs a fresh one.
_INSPECT_CACHE: weakref.WeakKeyDictionary[
    Callable[..., Any], tuple[inspect.Signature, dict[str, Any]]
] = weakref.WeakKeyDictionary()


def _inspect_handler(
    handler: Callable[..., Any],
    path_params: frozenset[str] = _NO_PATH_PARAMS,
) -> tuple[inspect.Signature, dict[str, Any]] | None:
    """Return `handler`'s signature and resolved type hints, or `None`.

    `None` means the callable has no inspectable signature - a builtin, or a C
    function. The two halves must describe the same object, and they do not
    follow the same indirection to find it (`get_type_hints` returns a class's
    *class-level* annotations), so the resolution is done by hand here first.
    PEP 593 metadata is kept, so a `Depends()` marker is detectable without the
    default-value form.
    """
    try:
        cached = _INSPECT_CACHE.get(handler)
    except TypeError:
        # An unhashable or non-weak-referenceable callable raises on the key.
        cached = None
    if cached is not None:
        return cached

    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return None

    hint_target: Any = resolve_hint_target(handler)

    try:
        hints = get_type_hints(hint_target, include_extras=True)
    except Exception as exc:
        # `get_type_hints` is all-or-none, so one unresolvable annotation would
        # take a `Depends()` marker with it. Not cached: it varies by route.
        return sig, _salvage_hints(hint_target, exc, path_params, sig.parameters)

    result = (sig, hints)
    with contextlib.suppress(TypeError):
        _INSPECT_CACHE[handler] = result
    return result


def _callable_name(obj: Any) -> str:
    """Name `obj` for an error or warning the user has to act on.

    `__qualname__` first, so a handler defined as a method or inside a factory
    keeps its enclosing context; then `__name__`, then `repr`.
    """
    return getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None) or repr(obj)


#: Parameters the plan binds from the name alone, plus the return annotation.
_BOUND_BY_NAME = frozenset({"request", "ws", "websocket", "return"})


def _salvage_hints(
    hint_target: Any,
    exc: Exception,
    path_params: frozenset[str],
    parameters: Mapping[str, inspect.Parameter],
) -> dict[str, Any]:
    """Resolve each annotation on its own, keeping the ones that succeed.

    Each goes through the same `get_type_hints` against the target's own
    globals, so a salvaged hint matches what the intact path would produce.
    """
    annotations = getattr(hint_target, "__annotations__", None) or {}
    globalns = getattr(hint_target, "__globals__", None)
    resolved: dict[str, Any] = {}
    unresolved: list[str] = []
    for name, annotation in annotations.items():
        probe = _AnnotationProbe()
        probe.__annotations__ = {name: annotation}
        try:
            resolved.update(get_type_hints(probe, globalns, include_extras=True))
        except Exception:  # noqa: BLE001 - this one genuinely cannot be resolved
            unresolved.append(name)
    # A parameter bound by NAME, and the return annotation, lose nothing here.
    consequential = [name for name in unresolved if name not in _BOUND_BY_NAME]
    if consequential:
        # `build_plan`'s signature: a partial's pre-bound parameter gets no slot.
        fatal = [
            name
            for name in consequential
            if _declaration_is_load_bearing(
                hint_target, annotations.get(name), name, parameters, path_params
            )
        ]
        if fatal:
            raise _unresolved_declaration_error(hint_target, fatal, exc)
        _warn_unresolved_annotations(hint_target, consequential, exc)
    return resolved


class _UnresolvedName:
    """Stands in for a name an annotation refers to that does not exist.

    Subscriptable and callable so the rest of the annotation still evaluates
    around it; the marker beside the unresolvable name is what matters.
    """

    __slots__ = ("name", "__metadata__")

    def __init__(self, name: str, metadata: tuple[Any, ...] = ()) -> None:
        self.name = name
        # Mirrors `Annotated`'s own attribute so the metadata scan reads one shape.
        self.__metadata__ = metadata

    def __getitem__(self, item: Any) -> _UnresolvedName:
        # The tail keeps the marker when `Annotated` is itself the missing name.
        tail = item[1:] if isinstance(item, tuple) else ()
        return _UnresolvedName(self.name, tail)

    def __call__(self, *args: Any, **kwargs: Any) -> _UnresolvedName:
        return self

    def __or__(self, other: Any) -> _UnresolvedName:
        # `X | None` must reach the same verdict as `Optional[X]`, not raise.
        return self

    __ror__ = __or__

    def __repr__(self) -> str:
        return f"<unresolved {self.name}>"


# A name moved under TYPE_CHECKING is most often a typing one, so resolving it
# lets the metadata scan judge it rather than refuse it unidentified.
_TYPING_NAMES: dict[str, Any] = {**vars(typing), **vars(typing_extensions)}


class _PlaceholderNamespace(dict[str, Any]):
    """A name mapping that yields a placeholder rather than raising `NameError`."""

    def __missing__(self, key: str) -> _UnresolvedName:
        return _UnresolvedName(key)


def _annotation_markers(hint_target: Any, annotation: Any) -> tuple[Any, tuple[Any, ...], bool]:
    """Return `(marker, metadata, known)` for an annotation that would not resolve.

    A string annotation is evaluated against the target's own globals with
    unresolvable names standing in as placeholders, so the marker beside the
    broken name is recovered as the object it is. Reading it by identity rather
    than by the text the author typed is what closes the rule: a scan for
    `"Security("` would miss `from veloce import Security as Guard`.

    `known` is False when the annotation could not be evaluated at all, and the
    caller refuses on that - not knowing is not a reason to admit.
    """
    live = annotation
    if isinstance(annotation, str):
        namespace = _PlaceholderNamespace(vars(builtins))
        namespace.update(_TYPING_NAMES)
        # The target's own globals are applied last so they always win.
        namespace.update(getattr(hint_target, "__globals__", None) or {})
        try:
            live = eval(annotation, {}, namespace)  # noqa: S307 - the target's own annotation
        except Exception:  # noqa: BLE001 - an annotation that will not evaluate tells us nothing
            return None, (), False

    marker, _base = extract_annotated_marker(live)
    carrier = live
    if carrier is not None and not hasattr(carrier, "__metadata__"):
        was_optional, inner = _unwrap_optional(carrier)
        if was_optional:
            carrier = inner
    return marker, tuple(getattr(carrier, "__metadata__", ())), True


def _declaration_is_load_bearing(
    hint_target: Any,
    annotation: Any,
    name: str,
    parameters: Any,
    path_params: frozenset[str] = _NO_PATH_PARAMS,
) -> bool:
    """Whether dropping this unresolved annotation would change what runs.

    Two properties fail closed, under one rule: the declaration decides where
    the value comes from, or what executes, so discarding it hands that decision
    to the caller. A dropped `Depends` / `Security` means the route answers
    without running the dependency; a dropped `Header()` / `Cookie()` makes a
    credential readable from the query string, where the caller supplies it and
    the access log records it.
    """
    from veloce.dependency import Depends  # local import breaks the import cycle

    marker, metadata, known = _annotation_markers(hint_target, annotation)
    if not known:
        return True
    if marker is not None:
        return True
    # A placeholder in a metadata slot means the marker's own name did not
    # resolve, and not knowing is no reason to serve.
    if any(isinstance(item, (Depends, ParamBase)) for item in metadata):
        return True
    if any(isinstance(item, _UnresolvedName) for item in metadata):
        return True
    # The fold carries a `Field`'s bound to resolve time, so the bound goes too.
    if any(field_info_declares_constraints(item) for item in metadata):
        return True
    if name in path_params:
        # Read from `path_params` first, so the declared source is unchanged.
        return False
    parameter = parameters.get(name)
    return parameter is not None and parameter.default is inspect.Parameter.empty


def _unresolved_declaration_error(hint_target: Any, fatal: list[str], exc: Exception) -> TypeError:
    """Build the error for an unresolved load-bearing annotation.

    Usually raised at route registration, but plans are also built on first use,
    so an override with a broken annotation raises here mid-request; the wording
    stays neutral about which.
    """
    where = _callable_name(hint_target)
    named = ", ".join(repr(n) for n in fatal)
    return TypeError(
        f"{where}: could not resolve the annotation on {named} "
        f"({type(exc).__name__}: {exc}). It is refused rather than dropped: "
        "a dropped `Depends()` / `Security()` marker "
        "answers the request without running the dependency, a dropped "
        "`Header()` / `Cookie()` marker moves the value to the query string "
        "where the caller supplies it, and a parameter with no default becomes "
        "a required query parameter. Import the name at runtime rather than "
        "only under TYPE_CHECKING."
    )


class _AnnotationProbe:
    """A throwaway carrier so one annotation can be resolved by itself.

    `get_type_hints` needs an object with `__annotations__`.
    """

    __slots__ = ("__annotations__",)


def _warn_unresolved_annotations(hint_target: Any, unresolved: list[str], exc: Exception) -> None:
    """Warn that some annotations could not be resolved, and nothing was lost.

    Only an annotation whose loss changes nothing reaches here; a load-bearing
    one raises in `_salvage_hints` instead.
    """
    where = _callable_name(hint_target)
    warnings.warn(
        f"{where}: could not resolve the annotation on "
        f"{', '.join(repr(n) for n in unresolved)} ({type(exc).__name__}: {exc}); "
        "those parameters are treated as unannotated. Nothing about where their "
        "values come from changes - import the name at runtime rather than only "
        "under TYPE_CHECKING to keep the annotation",
        _RegistrationWarning,
        stacklevel=3,
    )


def _extend_plan_chain(handler: Callable[..., Any], seen: list[Any] | None) -> list[Any]:
    """Append `handler` to the chain being planned, refusing a `Depends` cycle."""
    if seen is None:
        return [handler]
    for previous in seen:
        if previous is handler:
            chain = [_callable_name(c) for c in [*seen, handler]]
            raise ValueError(f"Circular dependency detected: {' -> '.join(chain)}")
    return [*seen, handler]


def _build_marker_slot(
    param_name: str,
    marker: ParamBase,
    annotation: Any,
    *,
    has_default: bool,
    websocket: bool,
) -> _Slot | None:
    """Build the slot for an explicit Query/Path/Header/Cookie/Body/Form/File marker.

    Returns `None` when the marker reads a request body a WebSocket handshake
    does not have, leaving the parameter to its handler default.
    """
    marker_kind = _marker_kind(marker)
    if websocket and marker_kind in (MK_BODY, MK_FORM, MK_FILE):
        return None
    # A model under query/header/cookie/form groups that source's fields instead
    # of naming one key; `Body`/`File` stay whole-body.
    grp_opt, grp_inner = _unwrap_optional(annotation) if annotation else (False, annotation)
    grp_backend = backend_of(grp_inner) if grp_inner else ModelBackend.NONE
    if (
        getattr(marker, "group", False)
        and grp_backend is not ModelBackend.NONE
        and marker_kind
        in (
            MK_QUERY,
            MK_HEADER,
            MK_COOKIE,
            MK_FORM,
        )
    ):
        slot = _Slot(K_MODEL_GROUP, param_name)
        slot.marker = marker
        slot.marker_kind = marker_kind
        slot.model = grp_inner
        slot.backend = grp_backend
        slot.is_optional = grp_opt
        slot.has_default = marker.has_default
        slot.group_fields = _group_field_specs(grp_inner, grp_backend, marker_kind)
        return slot

    slot = _Slot(K_PARAM_MARKER, param_name)
    slot.marker = marker
    slot.marker_kind = marker_kind
    slot.lookup_name = marker.alias or param_name
    # An un-aliased Header param spells `x_token` as `x-token` unless disabled.
    if (
        slot.marker_kind == MK_HEADER
        and not marker.alias
        and getattr(marker, "convert_underscores", True)
    ):
        slot.lookup_name = slot.lookup_name.replace("_", "-")
    slot.default_factory = marker.default_factory
    slot.has_default = marker.has_default
    is_opt, inner = _unwrap_optional(annotation) if annotation else (False, annotation)
    slot.is_optional = is_opt
    slot.target_type = inner if is_opt else annotation
    # The origin test excludes the sequence defaults rebuilt below.
    if needs_per_request_copy(marker.default) and get_origin(slot.target_type) not in (
        set,
        frozenset,
        tuple,
    ):
        _warn_shared_mutable_default(param_name, marker.default, stacklevel=4)
    # `payload: Payload = Body()` declares what a bare `payload: Payload` does.
    if slot.marker_kind == MK_BODY and slot.target_type is not None:
        backend = backend_of(slot.target_type)
        if backend is not ModelBackend.NONE:
            slot.model = slot.target_type
            slot.backend = backend
    # Only a list-reading marker needs either.
    slot.marker_reads_list = get_origin(slot.target_type) in _SEQUENCE_ORIGINS
    if slot.marker_reads_list:
        marker = _normalise_sequence_default(marker, slot.target_type)
        slot.marker = marker
        from veloce.dependency import _list_marker_checks  # local import breaks the import cycle

        slot.list_checks = _list_marker_checks(marker)
        # `["0"]` bounded as a string would skip the published `items.minimum`.
        args = get_args(slot.target_type)
        slot.list_inner = args[0] if args else str
    return slot


def _adopt_field_marker(
    slot: _Slot,
    marker: ParamBase | None,
    param_name: str,
    declares_constraints: bool = False,
) -> None:
    """Carry a source-less `Field` marker on a bare path-or-query slot.

    The marker names no source, so the resolver keeps its own path-then-query
    lookup and only asks the marker to validate what that found. `lookup_name`
    is set either way, so the resolver reads one attribute per request.
    """
    slot.lookup_name = param_name
    if marker is None:
        return
    slot.marker = marker
    slot.marker_validates = declares_constraints
    if marker.alias:
        slot.lookup_name = marker.alias
    # The marker folded in the parameter's own default, so it is authoritative.
    slot.has_default = marker.has_default
    slot.default_factory = marker.default_factory
    slot._static_default = marker.default if marker.default is not ... else None
    if slot.kind == K_QUERY_LIST:
        # Only a list slot reads these; a scalar would hold a copy nothing reads.
        from veloce.dependency import _list_marker_checks  # local import breaks the import cycle

        slot.list_checks = _list_marker_checks(marker)


def _build_annotated_slot(
    param_name: str,
    annotation: Any,
    default: Any,
    *,
    has_default: bool,
    websocket: bool,
    field_info: Any = None,
    owner: str = "",
) -> _Slot | None:
    """Build the slot for a parameter carrying neither `Depends` nor a marker.

    The binding follows from the annotation alone. `None` when it needs a
    request body a WebSocket handshake does not have. A `field_info` is carried
    as a source-less marker by the two value bindings, and warned on by the two
    body bindings, where no marker could carry it.
    """
    is_optional, inner_type = _unwrap_optional(annotation) if annotation else (False, annotation)
    is_list, list_inner = _unwrap_list(inner_type) if inner_type else (False, inner_type)

    # A WebSocket has no multipart body, so the parameter keeps its default.
    if annotation is UploadFile or (is_optional and inner_type is UploadFile):
        if websocket:
            return None
        if field_info is not None and field_info_declares_constraints(field_info):
            _warn_field_info_unused(param_name, "a file upload", owner)
        slot = _Slot(K_UPLOAD_FILE, param_name)
        slot.is_optional = is_optional
        slot.has_default = has_default
        slot._static_default = default if has_default else None
        return slot

    # A `list[Model]` body is a GenericAlias, so it falls through to the list branch.
    body_backend = backend_of(inner_type) if inner_type else ModelBackend.NONE
    if body_backend is not ModelBackend.NONE:
        if websocket:
            return None
        if field_info is not None and field_info_declares_constraints(field_info):
            _warn_field_info_unused(param_name, "a request-body model", owner)
        slot = _Slot(K_BODY_MODEL, param_name)
        slot.model = inner_type
        slot.backend = body_backend
        slot.is_optional = is_optional
        return slot

    # A SOURCE-LESS marker: choosing `Path` or `Query` needs the route template.
    field_marker = (
        ParamBase.from_field_info(field_info, default if has_default else ...)
        if field_info is not None
        else None
    )
    field_declares_bounds = field_info is not None and field_info_declares_constraints(field_info)
    if field_info is not None:
        uncarried = field_info_uncarried_constraints(field_info)
        if uncarried:
            _warn_field_constraint_uncarried(param_name, uncarried, owner)

    if is_list:
        slot = _Slot(K_QUERY_LIST, param_name)
        slot.list_inner = list_inner
        slot.has_default = has_default
        slot._static_default = default if has_default else None
        slot.is_optional = is_optional
        _guard_plain_mutable_default(slot, param_name, stacklevel=4)
        _adopt_field_marker(slot, field_marker, param_name, field_declares_bounds)
        return slot

    # Decided at resolve time, because `path_params` are scope-local per match.
    slot = _Slot(K_QUERY, param_name)
    slot.target_type = inner_type if inner_type is not None else str
    slot.is_optional = is_optional
    slot.has_default = has_default
    slot._static_default = default if has_default else None
    _guard_plain_mutable_default(slot, param_name, stacklevel=4)
    _adopt_field_marker(slot, field_marker, param_name, field_declares_bounds)
    return slot


def build_plan(
    handler: Callable[..., Any],
    *,
    websocket: bool = False,
    path_params: frozenset[str] = _NO_PATH_PARAMS,
    _seen: list[Any] | None = None,
) -> HandlerPlan:
    """Inspect `handler` and freeze a resolution plan.

    Called once per route registration, and safe on builtins, lambdas, partials
    or callable classes - a handler with no inspectable signature gets an empty
    plan. With `websocket` set, a parameter typed `WebSocket` (or named `ws` /
    `websocket`) binds to a `K_WEBSOCKET` slot. `_seen` carries the chain being
    planned, so a `Depends` cycle raises `ValueError` rather than recursing.
    """
    from veloce.dependency import Depends, SecurityScopes  # local import breaks the import cycle

    _seen = _extend_plan_chain(handler, _seen)

    ws_type: Any = None
    if websocket:
        ws_type = WebSocket

    inspected = _inspect_handler(handler, path_params)
    if inspected is None:
        return HandlerPlan(handler, [], [])
    sig, hints = inspected

    slots: list[_Slot] = []
    # The diagnostics below surface on one `router.py` line, which names no route.
    handler_name = _callable_name(handler)

    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue
        # *args / **kwargs are not injectable query parameters.
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue

        annotation = hints.get(param_name)
        default = param.default
        has_default = default is not inspect.Parameter.empty

        # PEP 593: a marker in the metadata is hoisted into the default position.
        field_info = _annotated_field_info(annotation)
        extracted_marker, annotation = extract_annotated_marker(annotation)
        if extracted_marker is not None and not isinstance(default, (Depends, ParamBase)):
            # `Annotated[int, Query()] = 10` declares its default beside the marker.
            if (
                has_default
                and isinstance(extracted_marker, ParamBase)
                and not extracted_marker.has_default
            ):
                extracted_marker = extracted_marker.with_default(default)
            default = extracted_marker
            has_default = True

        # A marker decides the parameter alone, so the `Field` has nothing to say.
        if field_info is not None and isinstance(default, (Depends, ParamBase)):
            if field_info_declares_constraints(field_info):
                _warn_field_info_overridden(param_name, default, handler_name)
            field_info = None

        # A by-name binding carrying a value marker would drop it silently.
        if isinstance(default, ParamBase):
            magic_name = param_name == "request" or (
                websocket and param_name in ("ws", "websocket")
            )
            if magic_name:
                _raise_kwarg_ambiguity(handler, param_name, default, _seen)

        # Checked first, so it wins over the request/path fallbacks below.
        if websocket and (annotation is ws_type or param_name in ("ws", "websocket")):
            slots.append(_Slot(K_WEBSOCKET, param_name))
            continue

        if param_name == "request" or annotation is Request:
            slots.append(_Slot(K_REQUEST, param_name))
            continue

        # A WebSocket handshake has no response cycle to attach tasks to.
        if annotation is BackgroundTasks:
            if not websocket:
                slots.append(_Slot(K_BG_TASKS, param_name))
            continue

        if annotation is Response:
            if not websocket:
                slots.append(_Slot(K_RESPONSE, param_name))
            continue

        if annotation is SecurityScopes:
            slots.append(_Slot(K_SECURITY_SCOPES, param_name))
            continue

        if isinstance(default, Depends):
            slots.append(
                _build_depends_slot(
                    param_name,
                    default,
                    inferred=annotation,
                    websocket=websocket,
                    path_params=path_params,
                    _seen=_seen,
                )
            )
            continue

        if isinstance(default, ParamBase):
            marker_slot = _build_marker_slot(
                param_name, default, annotation, has_default=has_default, websocket=websocket
            )
            if marker_slot is not None:
                slots.append(marker_slot)
            continue

        # A `Field` rides along as a source-less marker.
        annotated_slot = _build_annotated_slot(
            param_name,
            annotation,
            default,
            has_default=has_default,
            websocket=websocket,
            field_info=field_info,
            owner=handler_name,
        )
        if annotated_slot is not None:
            slots.append(annotated_slot)

    return HandlerPlan(handler, slots, [])


def build_route_dep_plans(
    route_dependencies: list[Any],
    *,
    websocket: bool = False,
    path_params: frozenset[str] = _NO_PATH_PARAMS,
) -> list[_Slot]:
    """Pre-plan a route's `dependencies=[Depends(...), ...]` list.

    A non-`Depends` entry is refused rather than skipped: dropped silently,
    `dependencies=[guard]` with the wrapper forgotten reads as protected and
    leaves every route open.
    """
    from veloce.dependency import Depends  # local import breaks the cycle

    out: list[_Slot] = []
    for dep in route_dependencies:
        if not isinstance(dep, Depends):
            raise TypeError(_dependency_entry_error(dep))
        out.append(_build_depends_slot("", dep, websocket=websocket, path_params=path_params))
    return out


def _dependency_entry_error(dep: Any) -> str:
    """Build the message for a `dependencies=` entry that is not a `Depends`."""
    if callable(dep):
        name = getattr(dep, "__name__", None) or "the callable"
        return (
            f"dependencies= takes Depends(...) or Security(...), not a bare callable; "
            f"got {name}. Wrap it as Depends({name}) so it runs."
        )
    return (
        f"dependencies= takes Depends(...) or Security(...); got {dep!r}, "
        f"which is not callable and cannot be a dependency."
    )
