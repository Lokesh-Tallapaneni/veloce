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

import builtins
import copy
import functools
import inspect
import types
import typing
import warnings
from collections.abc import Callable, Mapping
from typing import Any, Union, get_args, get_origin, get_type_hints

import typing_extensions

from veloce._internal import _is_async_callable
from veloce._model_backend import ModelBackend, _msgspec, backend_of
from veloce._params import Body, Cookie, File, Form, Header, ParamBase, Path
from veloce.background import BackgroundTasks
from veloce.http.datastructures import UploadFile
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.websocket import WebSocket

# `Depends` is imported lazily inside builders to break the dependency.py <->
# _handler_plan.py cycle: dependency.py imports the plan API at module load,
# and we are imported back via that chain.


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


def extract_annotated_marker(annotation: Any) -> tuple[Any, Any]:
    """Split `Annotated[T, Depends()/Query()/...]` into `(marker, T)`.

    Returns `(None, annotation)` when there is no Veloce marker in the metadata,
    so a bare annotation - or one carrying unrelated metadata - passes through
    untouched.

    Shared with the OpenAPI walkers rather than reimplemented there. They
    classified a parameter solely by `isinstance(param.default, Depends)`, which
    the `Annotated` spelling never sets, so an `Annotated[..., Security(...)]`
    route was *enforced* at runtime and published as unauthenticated. One
    resolver means the two doors cannot disagree again.

    Python 3.10's `get_type_hints` still applies the implicit-Optional rule that
    PEP 484 dropped in 3.11: a parameter defaulting to `None` comes back as
    `Optional[Annotated[T, marker]]`, so `Annotated` is no longer outermost and
    the marker would be missed - silently rebinding a `Body()` / `Header()` /
    `Cookie()` parameter to the query string. That wrapper is peeled here, so
    every supported version sees the same shape; on 3.11+ it is a no-op.
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
    elif backend is ModelBackend.ADAPTED:
        # A dataclass or TypedDict declares no aliases, so the wire name is the
        # field name and the annotation comes from the type's own hints.
        items = [(name, None, tp) for name, tp in get_type_hints(model).items()]
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
    handler: Callable[..., Any], param_name: str, marker: Any, seen: list[Any] | None
) -> None:
    """Raise on a by-name magic parameter that also carries a value marker.

    Builds the dependency chain (when the offending parameter is in a nested
    `Depends`) from `_seen` so the message points at the exact handler.
    """
    # local: breaks the exceptions <-> http.response cycle. `exceptions` imports
    # `JSONResponse`, and `http.datastructures` imports back from
    # `exceptions`, so importing it here at module scope leaves
    # `exceptions` half-built. It cannot simply be hoisted: it works only
    # when placed after the `http` imports, which isort reorders away.
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


def _warn_shared_mutable_default(name: str, default: Any, *, stacklevel: int = 3) -> None:
    """Warn (once at registration) about a marker's shared mutable default.

    `stacklevel` counts the frames between here and the registration call the
    author wrote, so a caller reached through an extra helper raises it by one
    and the warning keeps pointing at the same place.
    """
    warnings.warn(
        f"parameter {name!r} has a mutable {type(default).__name__} default that is "
        f"shared across requests; pass default_factory={type(default).__name__} so each "
        f"request gets its own value",
        stacklevel=stacklevel,
    )


def _guard_plain_mutable_default(slot: _Slot, name: str, *, stacklevel: int = 3) -> None:
    """Stop a plain `param: list = []` default from being shared across requests.

    A bare mutable default (`tags: list[str] = []`) is one object on the
    signature; returning it by identity lets one request's in-place mutation leak
    into the next. Install a `default_factory` that deep-copies the original so
    each request gets an independent value, and warn the author - the same DX
    nudge the explicit-marker path already gives. Markers carry their own
    `default` handling, so this only fires for slots holding a static default.
    """
    if slot.has_default and isinstance(slot._static_default, (list, dict, set)):
        original = slot._static_default
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
        self._static_default: Any = None
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

    @property
    def default(self) -> Any:
        """This slot's default value, fresh per call when it needs to be.

        `_guard_plain_mutable_default` wraps a bare `param: list = []` in a
        copying factory so one request's `.append` cannot reach the next, and
        leaves the raw field pointing at the original object. Every binder that
        reads that field directly therefore has to remember to check the factory
        first - and the MCP binder did not, so a mutable default was shared
        across tool calls while HTTP requests each got their own.

        Reading `slot.default`, the obvious spelling, now gives the safe value.
        The shared object is still reachable as `_static_default`, which is a
        deliberate act rather than an oversight.

        The interpreter and both compiled resolvers branch on `default_factory`
        before they need a value and read the raw field, so none of them pays
        for this property. Code generation MUST keep doing so: it snapshots the
        value into the generated namespace once, and a property read there would
        bake in a single materialised copy - relocating the bug rather than
        fixing it.
        """
        factory = self.default_factory
        return factory() if factory is not None else self._static_default


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
    # Read directly: all three are `__slots__` fields that `__init__` assigns
    # unconditionally, so the `getattr` defaults could never apply. Matches the
    # sibling `_cached_callables`, which already reads them this way.
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

    Batches every parallel-safe dependency regardless of declaration order or
    intervening non-dependency slots, so `dep_a, q: int = Query(), dep_b`
    resolves `dep_a` and `dep_b` concurrently. The waves are an ordered list; the resolver runs each wave's
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
        # not re-scan slot safety on every request.
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
#: Shared empty set so the common call allocates nothing and no mutable default
#: is introduced.
_NO_PATH_PARAMS: frozenset[str] = frozenset()


def _build_depends_slot(
    name: str,
    dep: Any,
    inferred: Any = None,
    *,
    websocket: bool = False,
    path_params: frozenset[str] = _NO_PATH_PARAMS,
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
    slot.sub_plan = build_plan(callable_, websocket=websocket, path_params=path_params, _seen=_seen)
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


def _inspect_handler(
    handler: Callable[..., Any],
    path_params: frozenset[str] = _NO_PATH_PARAMS,
) -> tuple[inspect.Signature, dict[str, Any]] | None:
    """Return `handler`'s signature and resolved type hints, or `None`.

    `None` means the callable has no inspectable signature - a builtin, or a C
    function - and the caller returns an empty plan for it.

    The two halves have to agree on *which object* they describe, and they do
    not follow the same indirection to find it. `inspect.signature`
    transparently follows class -> `__init__`, callable-instance -> `__call__`
    and `functools.partial` -> wrapped function; `get_type_hints` does not - on
    a class it returns the *class-level* annotations rather than `__init__`'s.
    So the same resolution is done by hand here before the hints are read, and
    doing it in one place is why it cannot come apart.

    `include_extras=True` keeps PEP 593 `Annotated[T, Depends(...)]` metadata in
    the result, so a dependency marker is detectable without the user writing
    the default-value form. A failure to resolve hints at all - forward
    references, private modules - degrades to `{}` rather than raising: slots
    that need an annotation then fall back to the parameter default.
    """
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return None

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
        return sig, get_type_hints(hint_target, include_extras=True)
    except Exception as exc:
        # `get_type_hints` resolves the whole signature or none of it, so one
        # unresolvable annotation used to take every other annotation with it -
        # and what goes with them is the PEP 593 metadata that carries
        # `Depends()` / `Security()`. A handler whose unrelated `x: "Typo"`
        # parameter did not resolve stopped authenticating and answered 200.
        # Resolve what can be resolved instead, so a broken annotation costs
        # only its own parameter, and say so rather than degrading in silence.
        return sig, _salvage_hints(hint_target, exc, path_params, sig.parameters)


#: Parameters the plan binds from the name alone (see `build_plan`), plus the
#: return annotation. An unresolved annotation on one of these changes nothing.
_BOUND_BY_NAME = frozenset({"request", "ws", "websocket", "return"})


def _salvage_hints(
    hint_target: Any,
    exc: Exception,
    path_params: frozenset[str],
    parameters: Mapping[str, inspect.Parameter],
) -> dict[str, Any]:
    """Resolve each annotation on its own, keeping the ones that succeed.

    Each is resolved through the same `get_type_hints` the whole-signature call
    uses - handed one annotation at a time against the target's own globals - so
    a salvaged hint is identical to what the intact path would have produced.
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
    # A parameter the plan binds by NAME - `request`, `ws` / `websocket`, and
    # the return annotation, which is not a parameter at all - loses nothing
    # when its annotation does not resolve, so warning about it would be noise
    # on handlers that behave correctly. Only the parameters whose binding
    # genuinely depended on the annotation are worth reporting.
    consequential = [name for name in unresolved if name not in _BOUND_BY_NAME]
    if consequential:
        # `parameters` is the signature `build_plan` iterates, not the unwrapped
        # function's: a `functools.partial`'s pre-bound parameter receives no
        # slot, so dropping its annotation cannot move where a value comes from.
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
    around it - `Annotated[Missing, Security(f)]`, `Missing[int]`, `Missing()`.
    The marker sitting beside the unresolvable name is what matters here, not
    the name itself.
    """

    __slots__ = ("name", "__metadata__")

    def __init__(self, name: str, metadata: tuple[Any, ...] = ()) -> None:
        self.name = name
        # Mirrors `Annotated`'s own attribute so the metadata scan reads one
        # shape whether or not the subscript base resolved.
        self.__metadata__ = metadata

    def __getitem__(self, item: Any) -> _UnresolvedName:
        # Returning `self` discarded whatever sat inside the subscript, so
        # `Annotated[T, Security(dep)]` lost its marker whenever `Annotated`
        # was itself the name that did not resolve - the shape ruff's TC003
        # produces by moving the import under TYPE_CHECKING.
        tail = item[1:] if isinstance(item, tuple) else ()
        return _UnresolvedName(self.name, tail)

    def __call__(self, *args: Any, **kwargs: Any) -> _UnresolvedName:
        return self

    def __or__(self, other: Any) -> _UnresolvedName:
        # `X | None` must reach the same verdict as `Optional[X]`; without this
        # the union raises and "cannot evaluate" refuses a parameter a default
        # makes harmless. Returning self carries any recovered metadata through
        # the union rather than dropping it.
        return self

    __ror__ = __or__

    def __repr__(self) -> str:
        return f"<unresolved {self.name}>"


# Built once: a name an author moved under TYPE_CHECKING is most often a typing
# one, and resolving it to the real object lets the metadata scan judge it
# instead of refusing because it could not be identified.
_TYPING_NAMES: dict[str, Any] = {**vars(typing), **vars(typing_extensions)}


class _PlaceholderNamespace(dict):
    """A name mapping that yields a placeholder rather than raising `NameError`."""

    def __missing__(self, key: str) -> _UnresolvedName:
        return _UnresolvedName(key)


def _annotation_markers(hint_target: Any, annotation: Any) -> tuple[Any, tuple[Any, ...], bool]:
    """Return `(marker, metadata, known)` for an annotation that would not resolve.

    A live `Annotated[...]` already carries its markers. A string - which is
    what `from __future__ import annotations` leaves behind, and the shape the
    marker object was never built for - is evaluated against the target's own
    globals with unresolvable names standing in as placeholders, so the marker
    beside the broken name is recovered as the object it actually is.

    Reading the marker by identity rather than by the text the author typed is
    what makes the rule closed. A scan for `"Security("` matches only that
    spelling: `from veloce import Security as Guard` evaded it, and with a
    default to sidestep the no-default rule the guard never ran and the caller
    supplied the value.

    `marker` comes from `extract_annotated_marker`, the module's one resolver,
    so `Optional[Annotated[T, marker]]` is peeled here as it is everywhere else
    - Python 3.10 produces that shape for any parameter defaulting to `None`,
    and a raw `__metadata__` read sees straight past it.

    `known` is False when the annotation could not be evaluated at all, and the
    caller refuses on that - not knowing is not a reason to admit. A name the
    namespace could not supply comes back as `_UnresolvedName`; the caller
    refuses when one occupies a metadata slot, for the same reason.

    The evaluation is the same one `get_type_hints` performs on the success
    path, against the same globals, so it introduces no execution the intact
    path would not already have done.
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

    Two properties fail closed, and both are instances of one rule: the
    declaration decides where the value comes from, or what executes, so
    discarding it hands that decision to the caller.

    A `Depends` / `Security` marker is a control - dropped, the route answers
    without ever running the dependency. Any other `ParamBase` marker
    (`Header`, `Cookie`, `Query`, `Body`, `Form`, `File`) names the source the
    value is read from - dropped, a credential declared header- or cookie-borne
    becomes readable from the query string, where the caller supplies it and
    the access log records it. A parameter with no default degrades into a
    *required* query parameter, which is the same bypass from the other side.

    The marker comes from `extract_annotated_marker`, which matches on the
    marker classes rather than an allowlist of names - so a marker added later
    is covered without anyone remembering to list it, and the `Optional[...]`
    wrapper Python 3.10 adds is peeled the same way it is everywhere else.
    """
    from veloce.dependency import Depends  # local import breaks the import cycle

    marker, metadata, known = _annotation_markers(hint_target, annotation)
    if not known:
        return True
    if marker is not None:
        return True
    # A placeholder standing where a marker would sit means the marker's own
    # name did not resolve either - `Annotated[X, Guard(dep)]` with `Guard`
    # imported inside an enclosing function, which `__globals__` cannot see.
    # What it was cannot be recovered, so it is refused: not knowing whether a
    # control was declared is not a reason to serve the route without one.
    if any(isinstance(item, (Depends, ParamBase)) for item in metadata):
        return True
    if any(isinstance(item, _UnresolvedName) for item in metadata):
        return True
    if name in path_params:
        # The name is in the route template, so the resolver reads it from
        # `path_params` before the query string - the source the declaration
        # named is the source it still has.
        return False
    parameter = parameters.get(name)
    return parameter is not None and parameter.default is inspect.Parameter.empty


def _unresolved_declaration_error(hint_target: Any, fatal: list[str], exc: Exception) -> TypeError:
    """Build the registration error for an unresolved load-bearing annotation."""
    where = getattr(hint_target, "__qualname__", None) or repr(hint_target)
    named = ", ".join(repr(n) for n in fatal)
    return TypeError(
        f"{where}: could not resolve the annotation on {named} "
        f"({type(exc).__name__}: {exc}). The route is refused rather than "
        "registered without it: a dropped `Depends()` / `Security()` marker "
        "answers the request without running the dependency, a dropped "
        "`Header()` / `Cookie()` marker moves the value to the query string "
        "where the caller supplies it, and a parameter with no default becomes "
        "a required query parameter. Import the name at runtime rather than "
        "only under TYPE_CHECKING."
    )


class _AnnotationProbe:
    """A throwaway carrier so one annotation can be resolved by itself.

    `get_type_hints` needs an object with `__annotations__`; giving it a fresh
    one per parameter is what turns an all-or-nothing resolution into a
    per-parameter one.
    """

    __slots__ = ("__annotations__",)


def _warn_unresolved_annotations(hint_target: Any, unresolved: list[str], exc: Exception) -> None:
    """Warn (once at registration) that some annotations could not be resolved.

    Silence here is what made the defect dangerous: the route simply stopped
    enforcing its security dependency and answered normally.
    """
    where = getattr(hint_target, "__qualname__", None) or repr(hint_target)
    warnings.warn(
        f"{where}: could not resolve the annotation on "
        f"{', '.join(repr(n) for n in unresolved)} ({type(exc).__name__}: {exc}); "
        "those parameters are treated as unannotated. Any `Depends()` / "
        "`Security()` metadata on them is not applied - import the name at "
        "runtime rather than only under TYPE_CHECKING",
        stacklevel=3,
    )


def _extend_plan_chain(handler: Callable[..., Any], seen: list[Any] | None) -> list[Any]:
    """Append `handler` to the chain being planned, refusing a `Depends` cycle.

    Entries are the callables themselves so the chain in the error reads
    naturally. Built lazily so external callers that call `build_plan(handler)`
    keep the original two-arg shape.
    """
    if seen is None:
        return [handler]
    for previous in seen:
        if previous is handler:
            # Prefer __qualname__ so lambdas and nested/method deps carry
            # scope context (e.g. `test_x.<locals>.<lambda>`) instead of
            # collapsing to bare `<lambda>` everywhere.
            chain = [
                getattr(c, "__qualname__", None) or getattr(c, "__name__", None) or repr(c)
                for c in [*seen, handler]
            ]
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
    # Body / Form / File markers read the HTTP request body, which
    # a WebSocket handshake does not have - skip them so the
    # handler default applies instead of crashing at resolve time.
    if websocket and marker_kind in (MK_BODY, MK_FORM, MK_FILE):
        return None
    # A model annotation under a query/header/cookie/form marker groups
    # that source's fields instead of naming a single key. `Body`/`File`
    # keep the existing whole-body binding.
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
        slot.has_default = has_default or marker.has_default
        slot.group_fields = _group_field_specs(grp_inner, grp_backend, marker_kind)
        return slot

    slot = _Slot(K_PARAM_MARKER, param_name)
    slot.marker = marker
    slot.marker_kind = marker_kind
    slot.lookup_name = marker.alias or param_name
    # An un-aliased Header param converts `_` -> `-`
    # in its name (`x_token` -> `x-token`) unless disabled.
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
    # DX lint (Veloce-original): a mutable
    # static default on a marker is shared across every request, so an
    # in-place mutation by one handler leaks into the next. Point the
    # author at `default_factory`, which builds a fresh value per call.
    # `default` and `default_factory` are mutually exclusive, so a
    # mutable `default` is exactly "the author wrote a static mutable
    # default". The marker copies it per request now; still point them
    # at the spelling that says so.
    if isinstance(marker.default, (list, dict, set)):
        _warn_shared_mutable_default(param_name, marker.default, stacklevel=4)
    # `payload: Payload = Body()` declares the same thing as a bare
    # `payload: Payload`, so it must validate the same way. Recorded here
    # rather than tested per request: the resolver reads one attribute
    # instead of calling `is_pydantic_model` on every body.
    if slot.marker_kind == MK_BODY and slot.target_type is not None:
        backend = backend_of(slot.target_type)
        if backend is not ModelBackend.NONE:
            slot.model = slot.target_type
            slot.backend = backend
    return slot


def _build_annotated_slot(
    param_name: str,
    annotation: Any,
    default: Any,
    *,
    has_default: bool,
    websocket: bool,
) -> _Slot | None:
    """Build the slot for a parameter carrying neither `Depends` nor a marker.

    The binding follows from the annotation alone: an upload, a model body, a
    list read from the query, or the path-or-query fallback. Returns `None` when
    the binding needs a request body a WebSocket handshake does not have.
    """
    is_optional, inner_type = _unwrap_optional(annotation) if annotation else (False, annotation)
    is_list, list_inner = _unwrap_list(inner_type) if inner_type else (False, inner_type)

    # UploadFile binding (with or without Optional). A WebSocket has no
    # multipart form body, so the parameter is left to its default.
    if annotation is UploadFile or (is_optional and inner_type is UploadFile):
        if websocket:
            return None
        slot = _Slot(K_UPLOAD_FILE, param_name)
        slot.is_optional = is_optional
        slot.has_default = has_default
        slot._static_default = default if has_default else None
        return slot

    # Scalar model body - a Pydantic BaseModel or a msgspec.Struct. A
    # WebSocket handshake has no request body to validate, so the parameter
    # is left to its default. A `list[Model]` body is a GenericAlias (not a
    # `type`), so it falls through to the query-list branch below unchanged.
    body_backend = backend_of(inner_type) if inner_type else ModelBackend.NONE
    if body_backend is not ModelBackend.NONE:
        if websocket:
            return None
        slot = _Slot(K_BODY_MODEL, param_name)
        slot.model = inner_type
        slot.backend = body_backend
        slot.is_optional = is_optional
        return slot

    # List-typed parameter: read from query as a list.
    if is_list:
        slot = _Slot(K_QUERY_LIST, param_name)
        slot.list_inner = list_inner
        slot.has_default = has_default
        slot._static_default = default if has_default else None
        slot.is_optional = is_optional
        _guard_plain_mutable_default(slot, param_name, stacklevel=4)
        return slot

    # Default fallback: path-or-query, decided at resolve time because
    # path_params are scope-local (per match). The slot is path-or-query
    # ambiguous; we pick K_QUERY and the resolver will prefer path_params
    # when the name is present there. This keeps the plan handler-local
    # (one plan per handler, reusable across overrides).
    slot = _Slot(K_QUERY, param_name)
    slot.target_type = inner_type if inner_type is not None else str
    slot.is_optional = is_optional
    slot.has_default = has_default
    slot._static_default = default if has_default else None
    _guard_plain_mutable_default(slot, param_name, stacklevel=4)
    return slot


def build_plan(
    handler: Callable[..., Any],
    *,
    websocket: bool = False,
    path_params: frozenset[str] = _NO_PATH_PARAMS,
    _seen: list[Any] | None = None,
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

    _seen = _extend_plan_chain(handler, _seen)

    ws_type: Any = None
    if websocket:
        ws_type = WebSocket

    inspected = _inspect_handler(handler, path_params)
    if inspected is None:
        return HandlerPlan(handler, [], [])
    sig, hints = inspected

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

        # PEP 593: `Annotated[T, Depends(...)]` or `Annotated[T, Query(...)]`.
        # If the metadata carries a marker and the user didn't ALSO set it as the
        # default, hoist the marker into `default` and reduce `annotation` to the
        # inner type.
        extracted_marker, annotation = extract_annotated_marker(annotation)
        if extracted_marker is not None and not isinstance(default, (Depends, ParamBase)):
            default = extracted_marker
            has_default = True

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
                    path_params=path_params,
                    _seen=_seen,
                )
            )
            continue

        # Explicit parameter markers (Query/Path/Header/Cookie/Body/Form/File).
        if isinstance(default, ParamBase):
            marker_slot = _build_marker_slot(
                param_name, default, annotation, has_default=has_default, websocket=websocket
            )
            if marker_slot is not None:
                slots.append(marker_slot)
            continue

        # Everything else binds from the annotation alone.
        annotated_slot = _build_annotated_slot(
            param_name, annotation, default, has_default=has_default, websocket=websocket
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

    An entry that is not a `Depends` (or a `Security`, which subclasses it) is
    refused rather than skipped. Dropping it silently is how `dependencies=[guard]`
    - the wrapper forgotten - registered without a word and then never ran: the
    source read as protected and every route was open. This runs from `add_route`,
    so the refusal lands on the line that declared the route.
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
