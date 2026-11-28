"""`HandlerPlan` and its extension points."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from veloce._internal import _is_async_callable
from veloce._plan_slot import _Slot, compute_dep_waves


# ── Handler plan ──────────────────────────────────────────
class HandlerPlan:
    """Frozen resolution plan for one handler, plus its dependency graph."""

    __slots__ = (
        "handler",
        "is_coro",
        "slots",
        "route_dep_plans",
        "positional_only",
        "invoke",
        "compiled_resolver",
        "compiled_graph_resolver",
        "dep_waves",
        "wave_trigger",
        "wave_members",
        "_extensions",
    )

    def __init__(
        self,
        handler: Callable[..., Any],
        slots: list[_Slot],
        route_dep_plans: list[_Slot],
        positional_only: tuple[str, ...] = (),
    ) -> None:
        self.handler = handler
        self.is_coro = _is_async_callable(handler)
        self.slots = slots
        # K_DEPENDS slots for side-effect deps that bind to no parameter.
        self.route_dep_plans = route_dep_plans
        # Names of POSITIONAL_ONLY parameters, in declaration order - resolved
        # kwargs still key on these names, but calling the handler must pass
        # them positionally (see `split_positional_only`).
        self.positional_only = positional_only
        # The handler itself, callable directly as `invoke(**kwargs)`, for a
        # coroutine function with no POSITIONAL_ONLY parameter; `None` for
        # every other shape, where a caller falls back to the general
        # dispatch path. Measured: this roughly halves per-call overhead on
        # the common shape; a wrapper built for the rare shapes measured no
        # better than their existing fallback, so none is built for them.
        self.invoke: Callable[..., Any] | None = (
            handler if self.is_coro and not positional_only else None
        )
        # Lazily populated by `get_plan_extension` - stays `None` for any plan
        # nothing ever asks a single-consumer extension of.
        self._extensions: dict[str, Any] | None = None
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


# ── Plan extension points ─────────────────────────────────────────
#: Computes a `key`-namespaced fact about a plan's handler, from its
#: signature, for a single consumer's own policy decision - never a fact
#: more than one consumer needs to operate the handler itself (that belongs
#: directly on `HandlerPlan`, like `positional_only`).
PlanExtensionProvider = Callable[[inspect.Signature, "HandlerPlan"], Any]
_plan_extension_providers: dict[str, PlanExtensionProvider] = {}


def register_plan_extension(key: str, provider: PlanExtensionProvider) -> None:
    """Register `provider` as the `key`-namespaced plan extension provider.

    Computed, for any `HandlerPlan`, the first time `get_plan_extension(plan,
    key)` asks for it. Raises `ValueError` if `key` is already registered -
    a plan-extension key has exactly one legitimate owner, the package that
    defines it, so a second registration is always a bug: an accidental
    double-import, or two unrelated packages colliding on the same key.
    """
    if key in _plan_extension_providers:
        raise ValueError(f"a plan extension provider is already registered for {key!r}")
    _plan_extension_providers[key] = provider


def unregister_plan_extension(key: str) -> None:
    """Remove a previously registered provider. No-op if `key` was never registered."""
    _plan_extension_providers.pop(key, None)


def get_plan_extension(plan: HandlerPlan, key: str) -> Any:
    """Return `plan`'s `key`-namespaced extension, computed once and cached.

    Returns `None` without calling the provider when the handler has no
    inspectable signature - the same case `build_plan` tolerates, so a
    provider must answer "nothing special" for it too. Reads a bare
    `inspect.signature(plan.handler)`, not `_inspect_handler`'s hint-salvage
    path, which needs `build_plan`'s original `path_params` and would wrongly
    reject a parameter `build_plan` already accepted.
    """
    if plan._extensions is None:
        plan._extensions = {}
    if key not in plan._extensions:
        provider = _plan_extension_providers[key]
        try:
            sig = inspect.signature(plan.handler)
        except (TypeError, ValueError):
            plan._extensions[key] = None
        else:
            plan._extensions[key] = provider(sig, plan)
    return plan._extensions[key]
