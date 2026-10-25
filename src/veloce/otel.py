"""OpenTelemetry tracing bridge — one server span per finished HTTP request.

An **optional** integration that registers exactly one
:meth:`Veloce.add_instrumentation` hook to emit a server span per request.

Only the OpenTelemetry **API** is required; the application supplies its own
configured SDK, ``TracerProvider``, and exporter. Install the extra with::

    pip install veloceframework[otel]

Then wire it up once at startup::

    from veloce import Veloce
    from veloce.otel import instrument_with_otel

    app = Veloce()
    instrument_with_otel(app)

Importing this module never requires OpenTelemetry: the ``opentelemetry``
import is guarded, and :func:`instrument_with_otel` raises a clear
:class:`ImportError` with an install hint when it is missing. It is not
re-exported from the top-level ``veloce`` package because importing it costs
~32 ms on top of ``import veloce`` (measured with ``python -X importtime``).

**Timing.** The instrumentation hook fires *after* the response is produced, so
the default mode's span is recorded retroactively but *backdated*: ``end_time``
is the instant dispatch returned (``RequestMetrics.end_time_ns``, taken before
any other hook runs, so a slow earlier hook cannot shift the window) and
``start_time`` is that end minus the measured duration. Being created after the
fact, it is rooted in a fresh, empty context - never the ambient OpenTelemetry
context active at emission time - so it never parents itself under unrelated
work on the same task.

**Distributed-trace continuation.** The inbound ``traceparent`` / ``tracestate``
headers ride on the metrics record and the parent context is extracted from them
via ``TraceContextTextMapPropagator``, so a request carrying an upstream trace
joins it; absent them the span is a clean root. Extraction is on the emit path,
which runs on every dispatch outcome, so continuation never depends on hook
ordering.

**Scope.** The default mode opens no span around the handler, so a span the
handler creates is not parented under it and an outbound call carries no
context. ``instrument_with_otel(app, live=True)`` lifts that: an ASGI-layer
wrapper opens a real span at request start and attaches it to the OpenTelemetry
context for the whole handler. It is opt-in because it costs an ASGI wrapper and
one context attach/detach per request.

**Streamed response bodies are not traced in the default mode.** The hook fires
*before* a streaming body is emitted, so the measured duration and status would
cover only response production and could not see a mid-stream failure; records
with :attr:`RequestMetrics.streamed` set are skipped rather than mis-timed. (A
``HEAD`` request never iterates its body, so it is not marked streamed even on a
streaming route.) ``live=True`` moves the span lifecycle onto the send path and
so times a streamed response end to end, skipping nothing.

**Span naming and cardinality.** The span is named for the matched route
*template* (``metrics.route``, e.g. ``/items/{id}``), which is low-cardinality
and safe to export. An unmatched request (``metrics.route`` is ``None`` for both
a ``404`` and a ``405``) gets a stable method-based fallback (``"HTTP GET"``) and
no ``http.route`` attribute. The concrete path is high-cardinality and
attacker-controlled, so it is never a span name or an exported attribute.
"""

from __future__ import annotations

import contextlib
import time
import warnings
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, cast

from veloce._pipeline import (
    PH_ASGI_WRAP,
    WRAP_ORDER_OTEL,
    FeatureSpec,
)
from veloce._protocol_constants import (
    ASGI_SCOPE_HTTP,
    HTTP_METHOD_CONNECT,
    HTTP_METHOD_DELETE,
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
    HTTP_METHOD_PATCH,
    HTTP_METHOD_POST,
    HTTP_METHOD_PUT,
    HTTP_METHOD_TRACE,
    build_trace_carrier,
)
from veloce.status import HTTP_500_INTERNAL_SERVER_ERROR

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce
    from veloce.instrumentation import RequestMetrics

# Its absence must still allow `import veloce.otel`; raised from the factory.
try:
    from opentelemetry import context as _otel_context
    from opentelemetry import trace as _otel_trace
    from opentelemetry.context import Context as _OtelContext
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator as _W3CPropagator,
    )

    _OTEL_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without opentelemetry
    # `Any`, not four suppressions: no checker can follow `_OTEL_IMPORT_ERROR`,
    # and that flag is what gates every use of these names.
    _otel_context = _otel_trace = cast("Any", None)
    _OtelContext = _W3CPropagator = cast("Any", None)  # type: ignore[misc]
    _OTEL_IMPORT_ERROR = exc


_INSTALL_HINT = (
    "instrument_with_otel requires OpenTelemetry, which is an optional "
    "dependency. Install it with: pip install veloceframework[otel]"
)

# On the registered hook, never a module global, so two apps can share a
# process and a second `instrument_with_otel(app)` still finds the bridge.
_BRIDGE_MARKER = "_veloce_otel_bridge"

# The only verbs a span name may carry; any other is attacker-controlled.
_STANDARD_METHODS = frozenset(
    {
        HTTP_METHOD_GET,
        HTTP_METHOD_HEAD,
        HTTP_METHOD_POST,
        HTTP_METHOD_PUT,
        HTTP_METHOD_PATCH,
        HTTP_METHOD_DELETE,
        HTTP_METHOD_OPTIONS,
        HTTP_METHOD_TRACE,
        HTTP_METHOD_CONNECT,
    }
)

# Lowercased bytes, as ASGI delivers them; the only two the propagator reads.
_TRACEPARENT_HEADER = b"traceparent"
_TRACESTATE_HEADER = b"tracestate"


def _existing_bridge(app: Veloce, stacklevel: int) -> Callable[..., Any] | None:
    """Return the app's already-registered otel bridge hook, or `None`.

    Scans the app's own hook list, never a process-global sentinel, so two apps
    sharing a process each keep their own bridge. `stacklevel` differs by entry
    point so the warning points at the original `instrument_with_otel` call.
    """
    for hook in app.instrumentation_hooks:
        if getattr(hook, _BRIDGE_MARKER, False):
            warnings.warn(
                "instrument_with_otel was already called on this app; "
                "ignoring the redundant call to avoid emitting duplicate "
                "server spans per request.",
                RuntimeWarning,
                stacklevel=stacklevel,
            )
            return hook
    return None


def _span_name(route: str | None, method: str) -> str:
    """Return the low-cardinality span name for a (route, method) pair.

    An unmatched request (``route`` is ``None`` for both a 404 and a 405) falls
    back to a method-based name, and an unrecognised - hence attacker-controlled
    - verb collapses to one constant. The concrete request path is never used.
    """
    if route is not None:
        return route
    if method in _STANDARD_METHODS:
        return f"HTTP {method}"
    return "HTTP other"


def _enrich_span(
    span: Any,
    metrics: RequestMetrics,
    on_span: Callable[[Any, RequestMetrics], None] | None,
) -> None:
    """Apply the standard HTTP attributes (and optional user enrichment) to a span.

    The caller owns the span's name and lifecycle; this only writes attributes
    onto a span that is already started and current.
    """
    route = metrics.route
    status_code = metrics.status_code
    span.set_attribute("http.request.method", metrics.method)
    if route is not None:
        span.set_attribute("http.route", route)
    span.set_attribute("http.response.status_code", status_code)
    span.set_attribute("duration_ms", metrics.duration_ms)
    if status_code >= HTTP_500_INTERNAL_SERVER_ERROR:
        span.set_status(_otel_trace.Status(_otel_trace.StatusCode.ERROR))
        # The class name only, never the traceback or the exception instance.
        if metrics.error_type is not None:
            span.set_attribute("error.type", metrics.error_type)
    # Last, so it can override the built-ins. Suppressed: enrichment must not
    # break the response cycle, and the span still ends cleanly.
    if on_span is not None:
        with contextlib.suppress(Exception):
            on_span(span, metrics)


def _trace_carrier_from_scope(scope: dict[str, Any]) -> dict[str, str] | None:
    """Pull the inbound W3C trace headers out of a raw ASGI scope, if any.

    ``None`` when the request carries no ``traceparent``, so callers can skip
    propagator extraction.
    """
    traceparent: str | None = None
    tracestate: str | None = None
    for name, value in scope.get("headers", ()):  # raw (bytes, bytes) tuples
        if name == _TRACEPARENT_HEADER:
            traceparent = value.decode("latin-1")
        elif name == _TRACESTATE_HEADER:
            tracestate = value.decode("latin-1")
    return build_trace_carrier(traceparent, tracestate)


class _LiveSpanMiddleware:
    """ASGI wrapper that opens a live server span and makes it the current context.

    The span is attached to the OpenTelemetry context so handler-created and
    outbound spans are its children, and ended and detached in a ``finally`` so
    each concurrent request balances its own token even when the app raises. Its
    name and attributes come from the paired enrichment hook, which runs inside
    dispatch. Non-HTTP scopes pass straight through.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        tracer: Any,
        propagator: Any,
    ) -> None:
        self._app = app
        self._tracer = tracer
        self._propagator = propagator

    async def __call__(
        self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]
    ) -> None:
        if scope.get("type") != ASGI_SCOPE_HTTP:
            await self._app(scope, receive, send)
            return
        carrier = _trace_carrier_from_scope(scope)
        parent = self._propagator.extract(carrier) if carrier else _OtelContext()
        # Provisional; the enrichment hook supplies the route template.
        span = self._tracer.start_span(
            _span_name(None, scope.get("method", "")),
            context=parent,
            kind=_otel_trace.SpanKind.SERVER,
        )
        token = _otel_context.attach(_otel_trace.set_span_in_context(span))
        try:
            await self._app(scope, receive, send)
        finally:
            # Unconditional, so the token never leaks into the next request.
            _otel_context.detach(token)
            span.end()


def _instrument_live(
    app: Veloce,
    tracer_provider: Any | None,
    *,
    on_span: Callable[[Any, RequestMetrics], None] | None,
    exclude_routes: Iterable[str] | None,
) -> Callable[..., Any]:
    """Install the live-span bridge: an ASGI wrapper plus an enrichment hook.

    Shares the idempotency, ``on_span`` and ``exclude_routes`` contract with the
    backdated mode.
    """
    # A second install would stack a wrapper; the deeper stacklevel is this frame.
    existing = _existing_bridge(app, stacklevel=3)
    if existing is not None:
        return existing

    tracer = _otel_trace.get_tracer(__name__, tracer_provider=tracer_provider)
    propagator = _W3CPropagator()

    def _enrich_live_span(metrics: RequestMetrics) -> None:
        # The wrapper made the live span current before dispatch ran; a no-op
        # span would drop these writes anyway.
        span = _otel_trace.get_current_span()
        if not span.is_recording():
            return
        span.update_name(_span_name(metrics.route, metrics.method))
        _enrich_span(span, metrics, on_span)

    setattr(_enrich_live_span, _BRIDGE_MARKER, True)
    app.add_instrumentation(_enrich_live_span, exclude_routes=exclude_routes)
    # OUTERMOST, so every other ASGI middleware's latency falls inside the span:
    # `WRAP_ORDER_OTEL` outranks the default order and sorts first in the phase.
    options = {"tracer": tracer, "propagator": propagator}
    app._register_feature_state(
        app._features,
        FeatureSpec(
            "otel.live_span",
            PH_ASGI_WRAP,
            enabled=lambda: True,
            build=lambda: [(_LiveSpanMiddleware, options)],
            order=WRAP_ORDER_OTEL,
        ),
    )
    return _enrich_live_span


def instrument_with_otel(
    app: Veloce,
    tracer_provider: Any | None = None,
    *,
    live: bool = False,
    on_span: Callable[[Any, RequestMetrics], None] | None = None,
    exclude_routes: Iterable[str] | None = None,
) -> Callable[..., Any]:
    """Bridge a Veloce app's instrumentation into OpenTelemetry spans.

    Registers a single :meth:`Veloce.add_instrumentation` hook that turns each
    finished non-streamed request into a ``SpanKind.SERVER`` span, named for the
    matched route template or a low-cardinality method-based fallback. Each span
    carries ``http.request.method``, ``http.route`` (only when a route matched),
    ``http.response.status_code`` and ``duration_ms``; a ``5xx`` marks the span
    error and records ``RequestMetrics.error_type`` as ``error.type`` - the
    exception's class name, never its traceback or instance.

    Pass ``on_span`` to enrich every emitted span: it is called as
    ``on_span(span, metrics)`` after the built-in attributes are set and before
    the span ends, only for spans actually emitted, and an exception it raises is
    suppressed so enrichment can never break a response.

    Pass ``exclude_routes`` (matched route *templates*, e.g.
    ``{"/health", "/metrics"}``) to suppress spans for noisy routes. The
    exclusion is applied on the low-cardinality template in Veloce's core
    instrumentation loop.

    Pass ``tracer_provider`` to source the tracer from a specific provider;
    otherwise the globally configured provider is used. The application owns SDK
    and exporter configuration - this only emits spans.

    Raises :class:`ImportError` with an install hint when OpenTelemetry is not
    installed. Returns the registered hook, so callers can hold a reference to it
    for tests or introspection.

    Idempotent: a second call on the same app emits a :class:`RuntimeWarning` and
    returns the already-registered hook rather than doubling every server span.
    The dedup state lives on the app's hook list, not a module global.

    Streamed responses are not traced in the default mode, whose span is
    backdated from the finished metrics record and parented under the extracted
    inbound trace context; see the module docstring for both.

    Pass ``live=True`` for the opt-in *live* mode: an ASGI-layer wrapper opens a
    real ``SpanKind.SERVER`` span at request start and attaches it to the
    OpenTelemetry context for the whole handler, so in-handler and outbound
    spans are its children and a streamed response *is* timed end to end. It
    carries the same idempotency, ``on_span`` and ``exclude_routes`` semantics,
    except that an excluded route still gets a context-carrying span and only
    its enrichment is skipped.
    """
    if _OTEL_IMPORT_ERROR is not None:
        raise ImportError(_INSTALL_HINT) from _OTEL_IMPORT_ERROR

    if live:
        return _instrument_live(
            app,
            tracer_provider,
            on_span=on_span,
            exclude_routes=exclude_routes,
        )

    # A duplicate hook would double every server span.
    existing = _existing_bridge(app, stacklevel=2)
    if existing is not None:
        return existing

    tracer = _otel_trace.get_tracer(__name__, tracer_provider=tracer_provider)
    SpanKind = _otel_trace.SpanKind
    propagator = _W3CPropagator()

    def _emit_span(metrics: RequestMetrics) -> None:
        # A streamed body is sent after this hook, so the record would mis-time it.
        if metrics.streamed:
            return
        span_name = _span_name(metrics.route, metrics.method)
        # Anchored to the end captured at dispatch completion, in integer ns.
        end_time_ns = metrics.end_time_ns
        end_time = end_time_ns if end_time_ns is not None else time.time_ns()
        start_time = end_time - int(metrics.duration_ms * 1_000_000)
        # Never the ambient context, which would parent it under unrelated work.
        carrier = metrics.parent_context
        if carrier:
            # A malformed `traceparent` raises, which must not abort the span.
            try:
                parent = propagator.extract(cast("dict[str, str]", carrier))
            except Exception:
                parent = _OtelContext()
        else:
            parent = _OtelContext()
        span = tracer.start_span(
            span_name,
            context=parent,
            kind=SpanKind.SERVER,
            start_time=start_time,
        )
        try:
            _enrich_span(span, metrics, on_span)
        finally:
            span.end(end_time=end_time)

    # Tagged so a later `instrument_with_otel(app)` finds it.
    setattr(_emit_span, _BRIDGE_MARKER, True)
    app.add_instrumentation(_emit_span, exclude_routes=exclude_routes)
    return _emit_span
