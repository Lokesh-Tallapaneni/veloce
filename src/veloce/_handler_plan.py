"""Handler plan — pre-computed parameter-resolution plan for a route handler.

Built once at route registration; consumed per request. Removes the per-request
cost of `inspect.signature` and `typing.get_type_hints` from the dispatch path.

The plan is a list of `_Slot` records - one per handler parameter - each tagged
with the source the request should be read from. The plan also carries
pre-planned route-level dependencies and the recursive plans for any
`Depends` sub-graph reachable from the handler.

Reflection happens at registration time only, never on the hot path.
"""

from __future__ import annotations

import copy
import functools
import inspect
import types
import warnings
from collections.abc import Callable
from typing import Any, Union, get_args, get_origin, get_type_hints

from veloce._internal import _is_async_callable
from veloce._model_backend import ModelBackend, _msgspec, backend_of
from veloce.background import BackgroundTasks
from veloce.http.datastructures import UploadFile
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.routing.params import Body, Cookie, File, Form, Header, ParamBase, Path

# `Depends` is imported lazily inside builders to break the dependency.py <->
# _handler_plan.py cycle: dependency.py imports the plan API at module load,
# and we are imported back via that chain.


# ── Slot-kind constants ───────────────────────────────────
# Bare integers (not IntEnum) for cheap branching in the request hot loop.
K_REQUEST = 0
K_BG_TASKS = 1
K_DEPENDS = 2
K_PARAM_MARKER = 3
K_PATH = 4
K_QUERY = 5
K_QUERY_LIST = 6
K_BODY_MODEL = 7
K_UPLOAD_FILE = 8
K_SECURITY_SCOPES = 11
K_RESPONSE = 12
K_WEBSOCKET = 13
# A model whose FIELDS are read from one request source (query/header/cookie/
# form) rather than the body - `Annotated[Filters, Query()]`. The field walk
# happens once at registration; per request it is one dict build + one validate.
K_MODEL_GROUP = 14

# Marker kinds (for K_PARAM_MARKER slots).
MK_QUERY = 0
MK_PATH = 1
MK_HEADER = 2
MK_COOKIE = 3
MK_BODY = 4
MK_FORM = 5
MK_FILE = 6

# Canonical MK_* -> openapi-style location string. Single-sourced here next to
# the MK_ constants so the interpreter (`dependency.py`) and the codegen
# (`_resolver_codegen.py`) read one map instead of maintaining divergent copies.
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
    # `Union[X, None]` has origin typing.Union; `X | None` (PEP 604) has origin
    # types.UnionType - both must be recognised.
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


def _group_field_specs(
    model: Any, backend: ModelBackend, mk: int
) -> tuple[tuple[str, str, bool], ...]:
    """Resolve a grouped model's fields to `(validate_key, lookup_key, is_list)`.

    Two names, because they differ: the value is READ from the source under
    `lookup_key` (an alias, or a `Header` field's hyphenated spelling) and
    VALIDATED under `validate_key` - an aliased field is populated by its
    alias, not by its Python name.

    Aliases, `Header`'s underscore conversion and list detection are all decided
    here at registration so the per-request path is a plain `dict` build.
    """
    specs: list[tuple[str, str, bool]] = []
    if backend is ModelBackend.PYDANTIC:
        items = [
            (name, getattr(f, "alias", None), getattr(f, "annotation", None))
            for name, f in model.model_fields.items()
        ]
    else:
        items = [(f.name, f.encode_name, f.type) for f in _msgspec.structs.fields(model)]
    for name, alias, annotation in items:
        validate_key = alias or name
        key = validate_key
        # An un-aliased `Header` field spells `x_token` on the wire as `x-token`,
        # matching what a scalar `Header()` marker does.
        if mk == MK_HEADER and not alias:
            key = key.replace("_", "-")
        origin = get_origin(annotation)
        specs.append((validate_key, key, origin in (list, set, tuple)))
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
    handler: Callable, param_name: str, marker: Any, seen: list | None
) -> None:
    """Raise on a by-name magic parameter that also carries a value marker.

    Builds the dependency chain (when the offending parameter is in a nested
    `Depends`) from `_seen` so the message points at the exact handler.
    """
    from veloce.exceptions import ConfigurationError

    marker_name = type(marker).__name__
    chain = ""
    # Only surface the chain for a nested dependency (more than just the
    # handler itself); a top-level handler already names itself below.
    if seen and len(seen) > 1:
        names = [
            getattr(c, "__qualname__", None) or getattr(c, "__name__", None) or repr(c)
            for c in seen
        ]
        chain = f" (in dependency chain {' -> '.join(names)})"
    where = (
        getattr(handler, "__qualname__", None)
        or getattr(handler, "__name__", None)
        or repr(handler)
    )
    raise ConfigurationError(
        f"parameter {param_name!r} on {where}{chain} is reserved for the injected "
        f"{param_name!r} object but also declares a {marker_name}() marker; the marker "
        f"would be silently ignored. Rename the parameter or drop the {marker_name}() "
        "marker to resolve the ambiguity."
    )


def _warn_shared_mutable_default(name: str, default: Any) -> None:
    """Warn (once at registration) about a marker's shared mutable default."""
    warnings.warn(
        f"parameter {name!r} has a mutable {type(default).__name__} default that is "
        f"shared across requests; pass default_factory={type(default).__name__} so each "
        f"request gets its own value",
        stacklevel=3,
    )


def _guard_plain_mutable_default(slot: _Slot, name: str) -> None:
    """Stop a plain `param: list = []` default from being shared across requests.

    A bare mutable default (`tags: list[str] = []`) is one object on the
    signature; returning it by identity lets one request's in-place mutation leak
    into the next. Install a `default_factory` that deep-copies the original so
    each request gets an independent value, and warn the author - the same DX
    nudge the explicit-marker path already gives. Markers carry their own
    `default` handling, so this only fires for slots holding a static default.
    """
    if slot.has_default and isinstance(slot.default, (list, dict, set)):
        original = slot.default
        slot.default_factory = lambda: copy.deepcopy(original)
        _warn_shared_mutable_default(name, original)


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
        "default",
        "default_factory",
        "has_default",
        "is_optional",
        "list_inner",
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
    )

    def __init__(self, kind: int, name: str) -> None:
        self.kind = kind
        self.name = name
        self.target_type: Any = None
        self.default: Any = None
        # Set only for marker slots that carry a `default_factory`; plain
        # path/query slots leave this None and read the static `default`.
        self.default_factory: Callable[[], Any] | None = None
        self.has_default = False
        self.is_optional = False
        self.list_inner: Any = None
        self.model: Any = None
        self.marker: ParamBase | None = None
        self.marker_kind = MK_QUERY
        self.lookup_name = ""
        # K_MODEL_GROUP only: `(field, lookup_key, is_list)` per model field,
        # resolved at registration so the request path does no introspection.
        self.group_fields: tuple[tuple[str, str, bool], ...] = ()
        self.sub_plan: HandlerPlan | None = None
        self.use_cache = True
        self.dep_callable: Callable | None = None
        self.dep_is_coro = False
        # Yield-style dependencies (the typed-DI "context-manager" pattern): the
        # resolver starts the generator, captures the first yielded value as
        # the dependency result, and runs teardown after the response.
        self.dep_is_gen = False
        self.dep_is_async_gen = False
        # Opt-in: route a blocking sync dependency through the thread pool
        # instead of calling it inline on the event loop.
        self.dep_offload = False
        # Whether this dependency's cached result depends on the active
        # Security() scope union. True only when the dependency carries its own
        # Security() scopes AND its sub-graph reads them (a `SecurityScopes`
        # parameter anywhere below). The resolver folds the active scopes into
        # the cache key for these slots so the same callable referenced with
        # different scope sets resolves as distinct entries; plain dependencies
        # keep the cheap identity-only cache key. Computed once at registration.
        self.scope_sensitive = False
        # Which model backend validates a K_BODY_MODEL slot - Pydantic or
        # msgspec, tagged at registration by `backend_of(inner_type)`.
        self.backend = ModelBackend.NONE


# ── Parallel-dependency grouping ──────────────────────────
def _slot_parallel_safe(slot: _Slot, seen_plans: set[int]) -> bool:
    """Whether a K_DEPENDS slot and its whole sub-graph are parallel-safe.

    Unsafe when the slot (or any K_DEPENDS below it) pushes Security() scopes
    or is a yield-style dependency - both touch shared resolver state whose
    ordering parallel execution would corrupt. A pure function of plan
    structure, so the grouping it drives is computed once at registration.
    Cycle-guarded via `seen_plans`.
    """
    if isinstance(slot.target_type, list) and slot.target_type:
        return False
    if getattr(slot, "dep_is_gen", False) or getattr(slot, "dep_is_async_gen", False):
        return False
    sub_plan = getattr(slot, "sub_plan", None)
    if sub_plan is None:
        return True
    plan_id = id(sub_plan)
    if plan_id in seen_plans:
        return True
    seen_plans.add(plan_id)
    for sub in getattr(sub_plan, "slots", ()):
        if sub.kind == K_DEPENDS and not _slot_parallel_safe(sub, seen_plans):
            return False
    return True


def parallel_group_end(slots: list[_Slot], start: int) -> int:
    """Index past the last K_DEPENDS sibling safely parallelisable with `start`.

    A run extends while each slot is K_DEPENDS, parallel-safe, and does not
    share a `use_cache=True` callable with an earlier slot in the run (which
    would race on the shared result cache). Returns `start + 1` when the run
    cannot grow, so the caller runs that slot sequentially.
    """
    n = len(slots)
    if start >= n:
        return start
    seen_cached: set[Any] = set()
    end = start
    while end < n:
        s = slots[end]
        if s.kind != K_DEPENDS:
            break
        if not _slot_parallel_safe(s, set()):
            break
        if s.use_cache:
            if s.dep_callable in seen_cached:
                break
            seen_cached.add(s.dep_callable)
        end += 1
    return end


def compute_parallel_groups(slots: list[_Slot]) -> dict[int, int]:
    """Precompute the parallel-dependency grouping for a slot list.

    Returns `{start_index: end_index}` for every contiguous K_DEPENDS run of
    two or more slots that may run concurrently. The resolver consults this
    map per request instead of re-deriving the grouping each time - the
    grouping depends only on the plan, never on request data.
    """
    groups: dict[int, int] = {}
    i = 0
    n = len(slots)
    while i < n:
        if slots[i].kind == K_DEPENDS:
            end = parallel_group_end(slots, i)
            if end > i + 1:
                groups[i] = end
                i = end
                continue
        i += 1
    return groups


def _cached_callables(slot: _Slot, seen_plans: set[int]) -> set[Any]:
    """Every `use_cache=True` callable reachable from a K_DEPENDS subtree.

    Two independent dependencies that share any cached callable (the slot's
    own, or one nested anywhere below it) must not run in the same wave: the
    first to resolve writes the shared result cache and the second reads it.
    Collecting the full reachable set - not just the top-level callable -
    catches the nested-shared-cache case the contiguous heuristic missed.
    Cycle-guarded via `seen_plans`.
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

    Unlike `compute_parallel_groups` (which only fuses a contiguous run of
    K_DEPENDS siblings in slot order), this batches every parallel-safe
    dependency regardless of declaration order or intervening non-dependency
    slots, so `dep_a, q: int = Query(), dep_b` resolves `dep_a` and `dep_b`
    concurrently. The waves are an ordered list; the resolver runs each wave's
    slots together and the waves themselves in sequence.

    Two dependencies sharing a cached callable anywhere in their subtrees are
    placed in successive waves (the shared callable acts as a synthetic
    prerequisite) so the cache is filled once and reused, never raced.
    Dependencies that are not parallel-safe (Security-scope mutators, yield
    deps) are excluded entirely - the resolver keeps running those inline in
    slot order to preserve teardown and scope semantics.

    Returns `[]` when fewer than two safe dependencies exist, so a linear
    chain or a single dep adds no per-request wave bookkeeping.
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
        # Earliest wave that does not already hold a slot sharing a cached
        # callable with this one. Walking waves in order keeps the synthetic
        # prerequisite ordering deterministic and slot-order-stable.
        for wave, taken in zip(waves, wave_cached, strict=True):
            if own_cached.isdisjoint(taken):
                wave.append(idx)
                taken |= own_cached
                placed = True
                break
        if not placed:
            waves.append([idx])
            wave_cached.append(set(own_cached))

    # A degenerate result where every wave holds one slot means nothing can
    # actually run concurrently; drop it so the resolver takes its plain path.
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
        "parallel_groups",
        "dep_waves",
        "wave_trigger",
        "wave_members",
    )

    def __init__(
        self,
        handler: Callable,
        slots: list[_Slot],
        route_dep_plans: list[_Slot],
    ) -> None:
        self.handler = handler
        self.is_coro = _is_async_callable(handler)
        self.slots = slots
        # Each entry is a K_DEPENDS slot; only used for side-effect deps that
        # do not bind to a handler parameter.
        self.route_dep_plans = route_dep_plans
        # Lazily-built straight-line resolver for param-only plans. `None` =
        # not yet attempted; a callable = compiled fast path; a sentinel =
        # tried and not compilable (see DependencyResolver.resolve_plan).
        self.compiled_resolver: Any = None
        # Lazily-built straight-line `async` resolver for a no-wave dependency
        # graph (see `_resolver_codegen.compile_graph_resolver`). Same tri-state
        # as `compiled_resolver`: `None` = not yet attempted; a callable =
        # compiled fast path; a sentinel = tried and not compilable.
        self.compiled_graph_resolver: Any = None
        # Parallel-dependency grouping, derived once here so the resolver does
        # not re-scan slot safety on every request. `parallel_groups` is the
        # legacy contiguous-run map (kept for the compat shims and external
        # callers); `dep_waves` is the topological batching the resolver now
        # drives, fusing independent deps across non-dependency slots.
        self.parallel_groups = compute_parallel_groups(slots)
        self.dep_waves = compute_dep_waves(slots)
        # Resolver-facing projection of the waves, built once here. Every batched
        # dependency is resolved at the earliest batched slot index
        # (`wave_trigger` keyed by that single index, valued with the ordered
        # list of waves to run sequentially). Running waves wave-by-wave there
        # guarantees a cache-prerequisite wave completes before the wave that
        # reuses its cached result. `wave_members` holds every batched index so
        # a member reached later in the loop is recognised as already resolved
        # and skipped. Both are empty when nothing batches, so a linear chain
        # pays no per-request overhead.
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
def _build_depends_slot(
    name: str,
    dep: Any,
    inferred: Any = None,
    *,
    websocket: bool = False,
    _seen: list | None = None,
) -> _Slot:
    """Build a K_DEPENDS slot, recursively planning the sub-callable.

    `Depends()` with no explicit dependency falls back to `inferred`
    (the parameter's type annotation) - the shorthand for
    `x: SomeClass = Depends()`. `websocket` is threaded down the chain so
    a dependency of a WebSocket handler can itself receive the
    `WebSocket` connection by annotation or `ws` / `websocket` name.
    """
    slot = _Slot(K_DEPENDS, name)
    slot.use_cache = dep.use_cache
    callable_ = dep.dependency if dep.dependency is not None else inferred
    slot.dep_callable = callable_
    slot.dep_is_coro = _is_async_callable(callable_)
    slot.dep_is_gen = inspect.isgeneratorfunction(callable_)
    slot.dep_is_async_gen = inspect.isasyncgenfunction(callable_)
    # `offload` only has meaning for a plain sync callable - a coroutine,
    # async generator, or sync generator already has its own execution
    # model, so the flag is recorded only when it can take effect.
    slot.dep_offload = bool(getattr(dep, "offload", False)) and not (
        slot.dep_is_coro or slot.dep_is_gen or slot.dep_is_async_gen
    )
    slot.sub_plan = build_plan(callable_, websocket=websocket, _seen=_seen)
    # Security() scopes flow down the chain so a `SecurityScopes`
    # parameter anywhere below sees the union. Plain `Depends` has no
    # scopes attribute; we read defensively.
    scopes = getattr(dep, "scopes", None)
    if scopes:
        # target_type is repurposed as the scope list for Security() slots.
        slot.target_type = list(scopes)
    # A dependency's cached result varies with the active scope union whenever
    # something in its sub-graph reads `SecurityScopes` - whether the scopes are
    # declared on this slot (`Security(..., scopes=...)`) OR inherited from an
    # ancestor Security() and read by a plain-`Depends` helper below it. Mark any
    # such slot so the resolver keys its cache by `(callable, active_scopes)`;
    # otherwise the same callable resolved under different scope sets collides.
    # One-time scan at registration; the hot path only branches on the bit.
    slot.scope_sensitive = _subgraph_reads_scopes(slot.sub_plan, set())
    return slot


def _subgraph_reads_scopes(plan: HandlerPlan | None, seen_plans: set[int]) -> bool:
    """Whether `plan` (or any dependency below it) reads `SecurityScopes`.

    A `K_SECURITY_SCOPES` slot anywhere in the reachable sub-graph means the
    dependency's result genuinely depends on the active scope union, so its
    cache entry must be scope-keyed. Cycle-guarded via `seen_plans`.
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


def build_plan(
    handler: Callable, *, websocket: bool = False, _seen: list | None = None
) -> HandlerPlan:
    """Inspect `handler` and freeze a resolution plan.

    Called exactly once per route registration. Safe to call on builtins,
    lambdas, partials, or callable classes - returns an empty plan for
    handlers without inspectable signatures.

    When `websocket` is set, a parameter typed `WebSocket` (or named `ws`
    / `websocket`) is bound to a `K_WEBSOCKET` slot instead of being read
    from the request - the same plan machinery then drives WebSocket
    dependency injection, giving it `yield`-teardown and `Security` parity
    with the HTTP path.

    `_seen` carries the chain of callables currently being planned so a
    `Depends` cycle (`A -> B -> A`) raises `ValueError` at registration
    time instead of recursing until the interpreter stack blows.
    """
    from veloce.dependency import Depends, SecurityScopes  # local import breaks the import cycle

    # Cycle guard - entries are the callables themselves so the chain in
    # the error reads naturally. Created lazily so external callers that
    # call `build_plan(handler)` keep the original two-arg shape.
    if _seen is None:
        _seen = [handler]
    else:
        for seen in _seen:
            if seen is handler:
                # Prefer __qualname__ so lambdas and nested/method deps carry
                # scope context (e.g. `test_x.<locals>.<lambda>`) instead of
                # collapsing to bare `<lambda>` everywhere.
                chain = [
                    getattr(c, "__qualname__", None) or getattr(c, "__name__", None) or repr(c)
                    for c in [*_seen, handler]
                ]
                raise ValueError(f"Circular dependency detected: {' -> '.join(chain)}")
        _seen = [*_seen, handler]

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
    # function indirection, but `get_type_hints` does not - on a class it
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
        # gracefully - slots that need annotations fall back to defaults.
        hints = {}

    slots: list[_Slot] = []

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

        # Python 3.10's `get_type_hints` still applies the implicit-Optional rule
        # that PEP 484 dropped in 3.11: a parameter defaulting to `None` comes
        # back as `Optional[Annotated[T, marker]]`, so `Annotated` is no longer
        # outermost and the marker below would be missed - silently rebinding a
        # `Body()` / `Header()` / `Cookie()` parameter to the query string. Peel
        # that wrapper so every supported version sees the same shape; on 3.11+
        # the wrapper is never added and this is a no-op.
        if annotation is not None and not hasattr(annotation, "__metadata__"):
            _was_optional, _inner_annotation = _unwrap_optional(annotation)
            if _was_optional and hasattr(_inner_annotation, "__metadata__"):
                annotation = _inner_annotation

        # PEP 593: `Annotated[T, Depends(...)]` or `Annotated[T, Query(...)]`.
        # If the metadata carries a `Depends` (or `ParamBase` marker) and
        # the user didn't ALSO set it as the default, hoist the marker
        # into `default` and reduce `annotation` to the inner type.
        if get_origin(annotation) is not None and hasattr(annotation, "__metadata__"):
            meta_args = get_args(annotation)
            base_type = meta_args[0] if meta_args else annotation
            metadata = getattr(annotation, "__metadata__", ())
            extracted_marker: Any = None
            for m in metadata:
                if isinstance(m, (Depends, ParamBase)):
                    extracted_marker = m
                    break
            if extracted_marker is not None and not isinstance(default, (Depends, ParamBase)):
                default = extracted_marker
                has_default = True
            annotation = base_type

        # Ambiguity guard (registration-time, intent-aware). A parameter bound
        # by a BY-NAME magic rule below (`request`, or `ws` / `websocket` on a
        # WebSocket plan) that ALSO carries an explicit Query / Path / Header /
        # Cookie / Body / Form / File marker is contradictory: the name asks for
        # the injected object while the marker asks for a request value, and the
        # by-name rule silently wins, dropping the marker. Flag only this exact
        # conflict so the mis-binding surfaces at startup instead of at runtime.
        # The annotation-driven magic bindings (`x: Request`, `bt: BackgroundTasks`)
        # are not name-sensitive and stay untouched, and `Depends` is allowed so a
        # dependency may still be named `request`; this keeps valid handlers
        # (`q: str = Query()`, `request: Request`) free of false positives.
        if isinstance(default, ParamBase):
            magic_name = param_name == "request" or (
                websocket and param_name in ("ws", "websocket")
            )
            if magic_name:
                _raise_kwarg_ambiguity(handler, param_name, default, _seen)

        # WebSocket injection (websocket plans only) - by annotation or by
        # the `ws` / `websocket` parameter name. Checked first so it wins
        # over the request/path fallbacks for a WebSocket handler.
        if websocket and (annotation is ws_type or param_name in ("ws", "websocket")):
            slots.append(_Slot(K_WEBSOCKET, param_name))
            continue

        # Request injection - either by name or by annotation.
        if param_name == "request" or annotation is Request:
            slots.append(_Slot(K_REQUEST, param_name))
            continue

        # BackgroundTasks injection by annotation. A WebSocket handshake
        # has no response cycle to attach tasks to, so the parameter is
        # left to its handler default rather than injected.
        if annotation is BackgroundTasks:
            if not websocket:
                slots.append(_Slot(K_BG_TASKS, param_name))
            continue

        # Response injection by annotation. The handler
        # receives a fresh Response it can mutate (status_code, headers,
        # cookies); the dispatcher merges those onto the final response.
        # There is no HTTP Response on a WebSocket route - skip it there.
        if annotation is Response:
            if not websocket:
                slots.append(_Slot(K_RESPONSE, param_name))
            continue

        # SecurityScopes - receives the accumulated Security() chain scopes.
        if annotation is SecurityScopes:
            slots.append(_Slot(K_SECURITY_SCOPES, param_name))
            continue

        # Depends() / Security() in default position. A bare `Depends()`
        # with no callable infers the dependency from the annotation
        # (`x: SomeClass = Depends()`).
        if isinstance(default, Depends):
            slots.append(
                _build_depends_slot(
                    param_name,
                    default,
                    inferred=annotation,
                    websocket=websocket,
                    _seen=_seen,
                )
            )
            continue

        # Explicit parameter markers (Query/Path/Header/Cookie/Body/Form/File).
        if isinstance(default, ParamBase):
            marker_kind = _marker_kind(default)
            # Body / Form / File markers read the HTTP request body, which
            # a WebSocket handshake does not have - skip them so the
            # handler default applies instead of crashing at resolve time.
            if websocket and marker_kind in (MK_BODY, MK_FORM, MK_FILE):
                continue
            # A model annotation under a query/header/cookie/form marker groups
            # that source's fields instead of naming a single key. `Body`/`File`
            # keep the existing whole-body binding.
            _grp_opt, _grp_inner = (
                _unwrap_optional(annotation) if annotation else (False, annotation)
            )
            _grp_backend = backend_of(_grp_inner) if _grp_inner else ModelBackend.NONE
            if (
                getattr(default, "group", False)
                and _grp_backend is not ModelBackend.NONE
                and marker_kind
                in (
                    MK_QUERY,
                    MK_HEADER,
                    MK_COOKIE,
                    MK_FORM,
                )
            ):
                slot = _Slot(K_MODEL_GROUP, param_name)
                slot.marker = default
                slot.marker_kind = marker_kind
                slot.model = _grp_inner
                slot.backend = _grp_backend
                slot.is_optional = _grp_opt
                slot.has_default = has_default or default.has_default
                slot.group_fields = _group_field_specs(_grp_inner, _grp_backend, marker_kind)
                slots.append(slot)
                continue

            slot = _Slot(K_PARAM_MARKER, param_name)
            slot.marker = default
            slot.marker_kind = marker_kind
            slot.lookup_name = default.alias or param_name
            # An un-aliased Header param converts `_` -> `-`
            # in its name (`x_token` -> `x-token`) unless disabled.
            if (
                slot.marker_kind == MK_HEADER
                and not default.alias
                and getattr(default, "convert_underscores", True)
            ):
                slot.lookup_name = slot.lookup_name.replace("_", "-")
            slot.default_factory = default.default_factory
            slot.has_default = default.has_default
            is_opt, inner = _unwrap_optional(annotation) if annotation else (False, annotation)
            slot.is_optional = is_opt
            slot.target_type = inner if is_opt else annotation
            # DX lint (Veloce-original): a mutable
            # static default on a marker is shared across every request, so an
            # in-place mutation by one handler leaks into the next. Point the
            # author at `default_factory`, which builds a fresh value per call.
            if default.default_factory is None and isinstance(default.default, (list, dict, set)):
                _warn_shared_mutable_default(param_name, default.default)
            slots.append(slot)
            continue

        is_optional, inner_type = (
            _unwrap_optional(annotation) if annotation else (False, annotation)
        )
        is_list, list_inner = _unwrap_list(inner_type) if inner_type else (False, inner_type)

        # UploadFile binding (with or without Optional). A WebSocket has no
        # multipart form body, so the parameter is left to its default.
        if annotation is UploadFile or (is_optional and inner_type is UploadFile):
            if not websocket:
                slot = _Slot(K_UPLOAD_FILE, param_name)
                slot.is_optional = is_optional
                slot.has_default = has_default
                slot.default = default if has_default else None
                slots.append(slot)
            continue

        # Scalar model body - a Pydantic BaseModel or a msgspec.Struct. A
        # WebSocket handshake has no request body to validate, so the parameter
        # is left to its default. A `list[Model]` body is a GenericAlias (not a
        # `type`), so it falls through to the query-list branch below unchanged.
        _body_backend = backend_of(inner_type) if inner_type else ModelBackend.NONE
        if _body_backend is not ModelBackend.NONE:
            if not websocket:
                slot = _Slot(K_BODY_MODEL, param_name)
                slot.model = inner_type
                slot.backend = _body_backend
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
            _guard_plain_mutable_default(slot, param_name)
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
        _guard_plain_mutable_default(slot, param_name)
        slots.append(slot)

    return HandlerPlan(handler, slots, [])


def build_route_dep_plans(route_dependencies: list[Any], *, websocket: bool = False) -> list[_Slot]:
    """Pre-plan a route's `dependencies=[Depends(...), ...]` list."""
    from veloce.dependency import Depends  # local import breaks the cycle

    out: list[_Slot] = []
    for dep in route_dependencies:
        if isinstance(dep, Depends):
            out.append(_build_depends_slot("", dep, websocket=websocket))
    return out
