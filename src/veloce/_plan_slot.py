"""The per-parameter slot record and parallel-dependency grouping."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from veloce._model_backend import ModelBackend
from veloce._params import (
    ParamBase,
)
from veloce._plan_kinds import K_DEPENDS, MK_QUERY

if TYPE_CHECKING:  # pragma: no cover
    from veloce._plan_core import HandlerPlan


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
