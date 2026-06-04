"""Compiled feature pipeline - one compile-once artifact for app-level features.

A `FeatureSpec` declares WHEN a feature is enabled, WHERE in the request/connect
pipeline it plugs in (a `PH_*` phase), and HOW to build its per-request callable.
At compile time the registry is iterated once: each `enabled()` predicate runs a
single time, enabled specs are bucketed by phase, sorted by `order`, and fused into
one slot per phase (`None` for an empty phase, the bare callable for one, a tuple for
several). The result is a frozen `CompiledPipeline` the dispatch core reads by slot,
with no registry lookup, no predicate eval, and no iteration over disabled features
on the hot path.

The compiled artifact also carries the three route-resolution fast-path booleans
(`has_mounted_apps` / `has_static_handlers` / `has_asgi_mounts`). These are not
pipeline phases - they gate scans inside route matching - but they ride the same
generation-counter invalidation, so they need no separate manual invalidation site.

Built lazily and recompiled only when the app's generation counter advances; in
production the counter freezes once the setup lock latches, so the pipeline compiles
exactly once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from veloce.app import Veloce

    # One middleware's handshake checks: `(is_host_allowed, is_websocket_origin_allowed)`,
    # either a bound `(str) -> bool` check or `None` when that middleware lacks it.
    WsCheck = Callable[[str], bool] | None
    WsHandshakeChecks = tuple[tuple[WsCheck, WsCheck], ...]

# -- Phase ids ------------------------------------------------
# Bare integers (not IntEnum) for cheap branching, mirroring the `K_*` slot-kind
# constants in `_handler_plan.py`. Each id is the ONE pipeline slot a feature
# occupies; a feature spanning two phases registers two specs.
PH_HTTP_PRE = 0  # request-phase middleware slot (before the route-match path)
PH_HTTP_POST = 1  # response phase - runs after `_run_after_hooks` (ordering frozen)
PH_HTTP_AROUND = 2  # wraps dispatch (`call_next`) - `@app.middleware("http")`
PH_HTTP_FINISH = 3  # post-response observation (instrumentation timing + metrics)
PH_WS_HANDSHAKE = 4  # ws connect gate (host / origin allow-lists)
PH_ASGI_WRAP = 5  # outermost ASGI wrapper (ASGI middleware, live-otel span)

# Number of phases - the compiler buckets over `range(_PH_COUNT)`.
_PH_COUNT = 6


class FeatureSpec:
    """One feature declaration: when enabled, which phase, how to build it.

    `enabled` is a zero-arg predicate evaluated once at compile time. `build`
    returns the per-request callable (or any phase-appropriate artifact) once,
    only when the feature is enabled. `order` is a stable sort key within a phase
    (higher runs earlier), mirroring middleware priority.
    """

    __slots__ = ("name", "phase", "enabled", "build", "order")

    def __init__(
        self,
        name: str,
        phase: int,
        enabled: Callable[[], bool],
        build: Callable[[], object],
        order: int = 0,
    ) -> None:
        self.name = name
        self.phase = phase
        self.enabled = enabled
        self.build = build
        self.order = order


class CompiledPipeline:
    """Frozen per-app compile output: one fused slot per phase plus route flags.

    Each phase slot is `None` (no enabled feature), a bare callable / artifact
    (one feature), or a tuple (several). The three `has_*` booleans precompute
    the route-resolution mount/static scans so route matching can gate on a flag
    instead of probing the live lists.
    """

    __slots__ = (
        "gen",
        "http_pre",
        "http_post",
        "http_around",
        "http_finish",
        "ws_handshake",
        "asgi_wrap",
        "has_mounted_apps",
        "has_static_handlers",
        "has_asgi_mounts",
    )

    # Slot annotations (no runtime cost - `__slots__` owns storage). Phase slots
    # hold `None`, a bare artifact, or a tuple; the flags are precomputed booleans.
    gen: int
    http_pre: object
    http_post: object
    http_around: object
    http_finish: object
    ws_handshake: WsHandshakeChecks | None
    asgi_wrap: object
    has_mounted_apps: bool
    has_static_handlers: bool
    has_asgi_mounts: bool


# Phase id -> the `CompiledPipeline` slot it fuses into. Kept beside the phase
# constants so adding a phase is one row here, not a scattered edit.
_PHASE_SLOTS = (
    "http_pre",
    "http_post",
    "http_around",
    "http_finish",
    "ws_handshake",
    "asgi_wrap",
)


def _fuse(artifacts: list[object]) -> object:
    """Collapse a phase's built artifacts: bare value for one, tuple for many."""
    if len(artifacts) == 1:
        return artifacts[0]
    return tuple(artifacts)


def compile_pipeline(app: Veloce) -> CompiledPipeline:
    """Compile the app's feature registry into a frozen `CompiledPipeline`.

    Iterates `app._features` once, evaluates each `enabled()` once, buckets the
    enabled specs by phase, sorts each bucket by descending `order`, and fuses
    each into its slot. Also stamps the build generation and the three
    route-resolution flags.
    """
    cp = CompiledPipeline()
    cp.gen = app._gen

    buckets: list[list[FeatureSpec]] = [[] for _ in range(_PH_COUNT)]
    for spec in app._features:
        if spec.enabled():
            buckets[spec.phase].append(spec)

    for phase, specs in enumerate(buckets):
        slot = _PHASE_SLOTS[phase]
        if not specs:
            setattr(cp, slot, None)
            continue
        # Descending `order` so a higher-priority feature runs earlier; Python's
        # sort is stable, so equal orders keep registration order.
        specs.sort(key=lambda s: -s.order)
        setattr(cp, slot, _fuse([s.build() for s in specs]))

    # Route-resolution fast-path flags - same `_gen` invalidation, no manual row.
    cp.has_mounted_apps = bool(app._mounted_apps)
    cp.has_static_handlers = bool(app._static_handlers)
    cp.has_asgi_mounts = bool(app._asgi_mounts)
    return cp


def build_ws_handshake_checks(app: Veloce) -> WsHandshakeChecks:
    """Pre-filter the WebSocket host / origin allow-list checks from middleware.

    Returns one `(is_host_allowed, is_websocket_origin_allowed)` pair per
    registered middleware that exposes at least one of the two checks, in
    `_middlewares` order. A missing check is `None`, mirroring the per-connect
    `getattr(mw, ..., None)` probe this replaces, so the handshake gate iterates
    a frozen tuple instead of scanning every middleware on every connect.
    """
    pairs: list[tuple[WsCheck, WsCheck]] = []
    for mw in app._middlewares:
        host_check = getattr(mw, "is_host_allowed", None)
        origin_check = getattr(mw, "is_websocket_origin_allowed", None)
        if host_check is not None or origin_check is not None:
            pairs.append((host_check, origin_check))
    return tuple(pairs)
