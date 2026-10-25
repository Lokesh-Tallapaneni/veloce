"""Compiled feature pipeline — one compile-once artifact for app-level features.

A `FeatureSpec` declares WHEN a feature is enabled, WHERE in the request/connect
pipeline it plugs in (a `PH_*` phase), and HOW to build its per-request callable.
The compiler fuses the enabled specs into one slot per phase, so the dispatch
core reads a slot with no registry lookup, predicate eval, or iteration over
disabled features. The route-resolution `has_*` flags ride along for the same
generation-counter invalidation.

Built lazily and recompiled only when the app's generation counter advances; in
production the counter freezes once the setup lock latches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veloce.middleware.base import Middleware

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from veloce.app import Veloce

    # `(is_host_allowed, is_websocket_origin_allowed)`, each `None` when absent.
    WsCheck = Callable[[str], bool] | None
    WsHandshakeChecks = tuple[tuple[WsCheck, WsCheck], ...]

    # `(middleware_class, options)`, instantiated as `cls(app, **options)`.
    AsgiWrapPair = tuple[Any, dict[str, Any]]

# ── Phase ids ─────────────────────────────────────────────
# Bare integers (not IntEnum) for cheap branching; one id is one slot.
PH_HTTP_PRE = 0  # request-phase middleware slot (before the route-match path)
PH_HTTP_POST = 1  # response phase - runs after `_run_after_hooks` (ordering frozen)
PH_HTTP_AROUND = 2  # wraps dispatch (`call_next`) - `@app.middleware("http")`
PH_HTTP_FINISH = 3  # post-response observation (instrumentation timing + metrics)
PH_WS_HANDSHAKE = 4  # ws connect gate (host / origin allow-lists)
PH_ASGI_WRAP = 5  # outermost ASGI wrapper (ASGI middleware, live-otel span)

# Above the standard `_asgi_middleware` spec, so the otel wrapper is OUTERMOST.
WRAP_ORDER_OTEL = 100

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

    `asgi_wrap` may hold several specs' lists, so it is typed `object` and
    normalised by `flatten_asgi_wrap`.
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
        "is_bare",
        "bp_url_procs",
    )

    # Slot annotations only - `__slots__` owns the storage, so these cost nothing.
    gen: int
    http_pre: tuple[Callable[..., Any], ...] | None
    http_post: tuple[Callable[..., Any], ...] | None
    http_around: tuple[Callable[..., Any], ...] | None
    http_finish: tuple[Callable[..., Any], ...] | None
    ws_handshake: WsHandshakeChecks | None
    asgi_wrap: object
    has_mounted_apps: bool
    has_static_handlers: bool
    has_asgi_mounts: bool
    # True when every feature the fast path skips is absent; rides `_gen`.
    is_bare: bool
    bp_url_procs: dict[str, list[Any]] | None


# Phase id -> slot, beside the constants so a new phase is one row.
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

    Each `enabled()` predicate is evaluated exactly once, here.
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
        # Descending `order`, stably, so equal orders keep registration order.
        specs.sort(key=lambda s: -s.order)
        setattr(cp, slot, _fuse([s.build() for s in specs]))

    # Route-resolution fast-path flags - same `_gen` invalidation, no manual row.
    cp.has_mounted_apps = bool(app._mounted_apps)
    cp.has_static_handlers = bool(app._static_handlers)
    cp.has_asgi_mounts = bool(app._asgi_mounts)
    # `None` when no blueprint registered one, so the fast path tests an attribute.
    cp.bp_url_procs = app._bp_url_value_preprocessors or None
    cp.is_bare = (
        cp.http_pre is None
        and cp.http_post is None
        and cp.http_around is None
        and cp.http_finish is None
        and not cp.has_mounted_apps
        and not cp.has_static_handlers
        and not cp.has_asgi_mounts
        and not app._before_request_hooks
        and not app._after_request_hooks
        and not app._bp_before_hooks
        and not app._bp_after_hooks
        and not app._teardown_request_hooks
        and not app._teardown_appcontext_hooks
        and not app._bp_teardown_hooks
        and not app._url_value_preprocessors
        and not app._middlewares
    )
    return cp


def flatten_asgi_wrap(slot: object) -> list[AsgiWrapPair]:
    """Flatten the PH_ASGI_WRAP slot into one ordered `(cls, options)` list.

    The ASGI stack builder wraps the result from the inside out, so the
    highest-`order` spec ends up outermost.
    """
    if slot is None:
        return []
    # One spec: `_fuse` returned the bare list it built.
    if isinstance(slot, list):
        return slot
    flat: list[AsgiWrapPair] = []
    for part in slot:  # type: ignore[attr-defined]
        flat.extend(part)
    return flat


def _implements(mw: Any, hook: str) -> bool:
    """True when `mw` supplies `hook` rather than inheriting `Middleware`'s no-op.

    Most middleware implements one phase, and an inherited hook left in the
    compiled chain is an awaited coroutine per request that returns `None`.
    """
    return getattr(type(mw), hook, None) is not getattr(Middleware, hook)


def build_response_middleware(app: Veloce) -> tuple[Callable[..., Any], ...]:
    """Build the response-phase chain: `process_response` bound methods, reversed.

    Used only when a request carries no per-route exclusion chain; a route with
    `exclude_middleware` falls back to the dynamic filtered walk.
    """
    return tuple(
        mw.process_response
        for mw in reversed(app._middlewares)
        if _implements(mw, "process_response")
    )


def build_request_middleware(app: Veloce) -> tuple[Callable[..., Any], ...]:
    """Build the request-phase chain: `process_request` bound methods, forward.

    Used only when a request carries no per-route exclusion chain; an excluded
    route uses the dynamic filtered chain.
    """
    return tuple(
        mw.process_request for mw in app._middlewares if _implements(mw, "process_request")
    )


def build_ws_handshake_checks(app: Veloce) -> WsHandshakeChecks:
    """Pre-filter the WebSocket host / origin allow-list checks from middleware.

    One `(is_host_allowed, is_websocket_origin_allowed)` pair, `None` per absent
    check, for each middleware exposing either - so the handshake gate iterates a
    frozen tuple instead of scanning every middleware on every connect.
    """
    pairs: list[tuple[WsCheck, WsCheck]] = []
    for mw in app._middlewares:
        host_check = getattr(mw, "is_host_allowed", None)
        origin_check = getattr(mw, "is_websocket_origin_allowed", None)
        if host_check is not None or origin_check is not None:
            pairs.append((host_check, origin_check))
    return tuple(pairs)
