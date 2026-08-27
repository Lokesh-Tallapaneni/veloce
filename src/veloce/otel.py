"""OpenTelemetry tracing bridge — one server span per finished HTTP request.

This is an **optional** integration that turns each finished HTTP request into
an OpenTelemetry server span using Veloce's existing instrumentation hook
(:meth:`Veloce.add_instrumentation`). It does not replace or rebuild that hook
- it registers exactly one hook that emits a span per request.

Only the OpenTelemetry **API** is required; the application supplies its own
configured SDK, ``TracerProvider``, and exporter. Install the extra with::

    pip install veloceframework[otel]

Then wire it up once at startup::

    from veloce import Veloce
    from veloce.otel import instrument_with_otel

    app = Veloce()
    instrument_with_otel(app)

Importing this module (or ``import veloce``) never requires OpenTelemetry: the
``opentelemetry`` import is guarded, and :func:`instrument_with_otel` raises a
clear :class:`ImportError` with an install hint when it is missing.

**Timing:** Veloce's instrumentation hook fires *after* the response is
produced - :meth:`Veloce._run_instrumentation` runs once the request is
finished, with the already-measured wall-clock duration. The span emitted here
is therefore recorded retroactively from the
:class:`~veloce.instrumentation.RequestMetrics` record, but it is *backdated*:
its ``end_time`` is the wall-clock instant captured the moment dispatch
returned (``RequestMetrics.end_time_ns``, taken *before* any other
instrumentation hook or ``request_finished`` receiver runs, so a slow earlier
hook cannot shift the window) and its ``start_time`` is that end minus the
measured duration - so the exported span covers the real request window rather
than the instant this bridge's own hook executes. Because the span is created
after the fact, it is rooted
in a fresh, empty context - never the ambient OpenTelemetry context active at
emission time - so it is always a clean server-root span and never accidentally
parents itself under unrelated work running on the same task.

**Distributed-trace continuation.** The framework carries the inbound
``traceparent`` / ``tracestate`` headers on the metrics record, and this bridge
extracts a parent context from them via ``TraceContextTextMapPropagator`` when
it emits the span - so a request arriving with an upstream trace joins it (same
``trace_id``, parented under the caller's span) rather than starting a
disconnected root. A request with no trace headers yields an empty context and
the span is a clean root. Extraction happens on the span-emit path, which runs
on every dispatch outcome (success, an earlier ``before_request`` short-circuit,
or an error), so continuation never depends on hook ordering.

**Scope.** This is a *server-span* bridge: it continues an inbound trace and
emits one server span per request, but it does not inject context into
*outbound* calls your handler makes, nor does it open a live span that wraps
handler execution for fine-grained child spans - for that you would instrument
at the call site / ASGI layer. The span is driven off the same low-cardinality
dimensions a metrics exporter consumes.

**Streamed response bodies are not traced.** For a streaming body
(:class:`~veloce.http.response.StreamingResponse`,
:class:`~veloce.sse.EventSourceResponse`, a chunked
:class:`~veloce.http.response.FileResponse`) the instrumentation hook fires
*before* the body is emitted on the ASGI send path. The measured duration and
status would cover only the time to produce the response object - not the time
to drain the stream, and not a failure raised mid-stream - so backdating a span
from them would mis-time the request and hide stream errors. This bridge
therefore skips records where :attr:`RequestMetrics.streamed` is set and emits
no span for them. (A ``HEAD`` request never iterates its body - the ASGI path
sends headers and an empty terminal frame - so it is *not* marked streamed even
on a streaming route, and is traced normally.) Closing a span accurately
around a stream would require
moving the span lifecycle onto the ASGI send path so it ends after the stream
completes or fails; that is out of scope for this metrics-driven bridge.

**Span naming and cardinality.** The span is named for the matched route
*template* (``metrics.route``, e.g. ``/items/{id}``), which is low-cardinality
and safe to export. When no route matched (a ``404`` for an unknown path or a
``405`` for a disallowed method, where ``metrics.route`` is ``None``) the span
is named with a stable method-based fallback (``"HTTP GET"``) and carries no
``http.route`` attribute. The concrete request path (``metrics.path``) is
high-cardinality and attacker-controlled for unmatched requests, so it is never
used as a span name or exported as an attribute by default.
``instrument_with_otel`` is deliberately not re-exported from the top-level
``veloce`` package. Importing this module costs ~32 ms on top of
``import veloce`` (measured with ``python -X importtime``), which every
application would otherwise pay whether or not it emits traces. Import it from
``veloce.otel`` when you want it.
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

# OpenTelemetry is optional. Import its tracing API at module load so the span
# emission below can reference it, but tolerate its absence: a box without the
# extra must still be able to `import veloce` and `import veloce.otel`. When
# unavailable we record the sentinel error and gate the real requirement behind
# `instrument_with_otel`, where a precise ImportError with an install hint is
# raised.
try:
    from opentelemetry import context as _otel_context
    from opentelemetry import trace as _otel_trace
    from opentelemetry.context import Context as _OtelContext
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator as _W3CPropagator,
    )

    _OTEL_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without opentelemetry
    _otel_context = None  # type: ignore[assignment]
    _otel_trace = None  # type: ignore[assignment]
    _OtelContext = None  # type: ignore[assignment,misc]
    _W3CPropagator = None  # type: ignore[assignment,misc]
    _OTEL_IMPORT_ERROR = exc


_INSTALL_HINT = (
    "instrument_with_otel requires OpenTelemetry, which is an optional "
    "dependency. Install it with: pip install veloceframework[otel]"
)

# Marker attribute set on the registered span-emit hook so a second
# `instrument_with_otel(app)` call (a re-imported factory, a test fixture, a
# per-worker bootstrap) can detect the existing bridge instead of appending a
# duplicate that would emit two server spans per request. The state lives on the
# hook object registered on the app instance - never a module global - so it is
# correct when two apps share a process.
_BRIDGE_MARKER = "_veloce_otel_bridge"

# Recognised HTTP methods (RFC 9110 + PATCH/RFC 5789, plus Veloce's TRACE).
# Only these may appear in a span name; any other verb is attacker-controlled
# and collapses to "HTTP other" so it cannot explode span-name cardinality.
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

# Raw ASGI header names whose values seed the inbound W3C trace carrier. ASGI
# delivers header names lowercased as bytes; these are the only two the
# propagator reads, so the live wrapper extracts just them rather than building
# the whole header map per request.
_TRACEPARENT_HEADER = b"traceparent"
_TRACESTATE_HEADER = b"tracestate"


def _existing_bridge(app: Veloce, stacklevel: int) -> Callable[..., Any] | None:
    """Return the app's already-registered otel bridge hook, or `None`.

    Both the backdated and live factories must refuse a second install (a
    duplicate would emit two server spans / stack a second ASGI wrapper per
    request). The guard scans the app's own hook list - never a process-global
    sentinel - so two apps sharing a process each keep their own bridge. When a
    bridge is found it warns and returns it; the caller hands it back unchanged.
    The `stacklevel` differs by entry point so the warning points at the
    original `instrument_with_otel` call.
    """
    for hook in app._instrumentation:
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

    The matched route template is used when present; an unmatched request
    (``route`` is ``None`` for both a 404 and a 405) falls back to a stable
    method-based name, and an unrecognised - hence attacker-controlled - verb
    collapses to a single constant so the span name can never explode
    span-name cardinality. The concrete request path is never used.
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

    Shared by the backdated and live modes so both set an identical attribute set
    (method, route when matched, status, duration, `error.type` on a raised 5xx)
    and run the same suppressed ``on_span`` callback. The caller owns the span's
    name and lifecycle - this only writes attributes onto a span that is already
    started and current.
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
        # When the server error came from an unhandled raised exception, the core
        # records its low-cardinality class name. Surface it as the OpenTelemetry
        # `error.type` attribute - the class name only, never the traceback or
        # the exception instance.
        if metrics.error_type is not None:
            span.set_attribute("error.type", metrics.error_type)
    # Optional user enrichment, last so it can read/override the built-in
    # attributes. A raised enrichment callback must not break the response cycle
    # nor leak through the instrumentation hook, so it is suppressed and the span
    # still ends cleanly.
    if on_span is not None:
        with contextlib.suppress(Exception):
            on_span(span, metrics)


def _trace_carrier_from_scope(scope: dict[str, Any]) -> dict[str, str] | None:
    """Pull the inbound W3C trace headers out of a raw ASGI scope, if any.

    Returns a ``{"traceparent": ..., "tracestate": ...}`` carrier dict the
    propagator can extract, or ``None`` when the request carries no
    ``traceparent``. ASGI delivers header names lowercased as bytes; only the
    two trace headers are read so the wrapper never builds the whole header map.
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

    For an HTTP request it starts a ``SpanKind.SERVER`` span (parented under the
    inbound W3C trace when present), attaches it to the OpenTelemetry context so
    handler-created and outbound spans are its children, then ends the span and
    detaches the context token in a ``finally`` - so the token is balanced and
    never leaked even when the downstream app raises, and each concurrent request
    attaches/detaches its own token. The span's name and attributes are filled in
    by the paired enrichment hook, which runs inside dispatch while this span is
    current. Non-HTTP scopes (websocket, lifespan) pass straight through.
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

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope.get("type") != ASGI_SCOPE_HTTP:
            await self._app(scope, receive, send)
            return
        carrier = _trace_carrier_from_scope(scope)
        parent = self._propagator.extract(carrier) if carrier else _OtelContext()
        # Start the span with a provisional method-based name; the enrichment
        # hook updates it to the matched route template once routing is known.
        span = self._tracer.start_span(
            _span_name(None, scope.get("method", "")),
            context=parent,
            kind=_otel_trace.SpanKind.SERVER,
        )
        token = _otel_context.attach(_otel_trace.set_span_in_context(span))
        try:
            await self._app(scope, receive, send)
        finally:
            # Detach and end unconditionally so the context token is balanced on
            # every path - success, handler exception, or a mid-stream failure -
            # and never leaks into the next request handled on this task.
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

    The wrapper opens the live server span and owns its lifecycle; the
    enrichment hook (registered on ``add_instrumentation``) runs inside dispatch
    while the live span is current and updates its name/attributes from the
    finished ``RequestMetrics``. Shares the idempotency, ``on_span`` and
    ``exclude_routes`` contract with the backdated mode.
    """
    # Idempotency: a second live install would stack a second ASGI wrapper and a
    # second enrichment hook. Bail on the existing bridge with the same warning
    # the backdated mode uses (deeper stacklevel for the extra `_instrument_live`
    # frame).
    existing = _existing_bridge(app, stacklevel=3)
    if existing is not None:
        return existing

    tracer = _otel_trace.get_tracer(__name__, tracer_provider=tracer_provider)
    propagator = _W3CPropagator()

    def _enrich_live_span(metrics: RequestMetrics) -> None:
        # The wrapper attached the live server span as the current span before
        # dispatch ran, so this hook (which fires inside dispatch) sees it via
        # the ambient context. Enrich a recording span only - a no-op span (no
        # SDK/sampler configured) ignores the writes anyway, but skipping keeps
        # the hot path clean.
        span = _otel_trace.get_current_span()
        if not span.is_recording():
            return
        span.update_name(_span_name(metrics.route, metrics.method))
        _enrich_span(span, metrics, on_span)

    _enrich_live_span._veloce_otel_bridge = True  # type: ignore[attr-defined]
    app.add_instrumentation(_enrich_live_span, exclude_routes=exclude_routes)
    # Install the live span wrapper OUTERMOST so it wraps every other ASGI
    # middleware: the server span must exist before any of them run, and their
    # latency must fall inside it. The wrapper is registered as a PH_ASGI_WRAP
    # feature with `WRAP_ORDER_OTEL`, which is higher than the standard ASGI
    # middleware spec's default order, so it sorts first within the phase and is
    # composed outermost - the same position the historical
    # `_asgi_middleware.insert(0, ...)` gave it, but now reflected by the
    # generation counter so the assembled stack rebuilds without a manual reset.
    # `add_instrumentation` above already enforced the setup lock.
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
    finished non-streamed request into a ``SpanKind.SERVER`` span. The span name
    is the matched route template when available (``metrics.route``); when no
    route matched it falls back to a stable, low-cardinality method-based name
    (``"HTTP GET"``) and the concrete path is never used as a name or attribute.
    Each span carries ``http.request.method``, ``http.route`` (only when a route
    matched), ``http.response.status_code``, and a ``duration_ms`` attribute; a
    ``5xx`` status marks the span error. When the ``5xx`` came from an unhandled
    raised exception, ``RequestMetrics.error_type`` carries that exception's
    class name and the bridge records it as the OpenTelemetry ``error.type``
    span attribute - without ever touching the traceback or exception instance.

    Pass ``on_span`` to enrich every emitted span: it is called as
    ``on_span(span, metrics)`` inside the bridge's ``try``/``finally`` (after the
    built-in attributes are set, before the span ends) so a caller can add
    custom attributes or events without forking the bridge. It runs only for
    spans that are actually emitted (never for skipped streamed records). An
    exception raised by ``on_span`` is suppressed - enrichment can never break a
    response or leak through the instrumentation hook - and the span still ends
    cleanly.

    Pass ``exclude_routes`` (a set of matched route *templates*, e.g.
    ``{"/health", "/metrics"}``) to suppress spans for noisy routes; the
    exclusion is applied in Veloce's core instrumentation loop on the
    low-cardinality template, so health checks and scrape endpoints never
    pollute the trace stream.

    Streamed responses are not traced: their body is emitted after the hook
    fires, so the available timing/status would be wrong. Such records are
    skipped and emit no span. See the module docstring.

    Pass ``tracer_provider`` to source the tracer from a specific provider;
    otherwise the globally configured provider is used. The application owns SDK
    and exporter configuration - this only emits spans.

    Raises :class:`ImportError` with an install hint when OpenTelemetry is not
    installed. Returns the registered hook (which is also the value
    :meth:`Veloce.add_instrumentation` returns), so callers can hold a reference
    to it for tests or introspection.

    Continues an inbound W3C distributed trace: the request's ``traceparent`` /
    ``tracestate`` headers are carried on the metrics record and this bridge
    extracts a parent context from them when emitting the span, so an upstream
    trace is joined when present; absent those headers the span is a clean root.
    Extraction is on the emit path (not a skippable ``before_request`` hook), so
    it works even for a request short-circuited by an earlier hook.

    The span is recorded retroactively from the request's metrics record, not
    as a live wrap of handler execution, but it is backdated: ``start_time`` and
    ``end_time`` are set from the measured duration so the exported span covers
    the real request window. It is parented under the extracted inbound context
    (or a fresh empty one), never the ambient OpenTelemetry context active when
    the hook fires.

    Idempotent: calling this more than once on the same app (a re-imported
    factory, a test fixture, a per-worker bootstrap) does not register a second
    bridge - which would emit two server spans per request and double export
    cost. A redundant call emits a :class:`RuntimeWarning` and returns the
    already-registered hook unchanged. The dedup state lives on the app's hook
    list, not a module global, so two apps in one process each get their own
    bridge.

    Pass ``live=True`` for the opt-in *live* mode: instead of backdating a span
    after the request finishes, an ASGI-layer wrapper opens a real
    ``SpanKind.SERVER`` span at request start and attaches it to the
    OpenTelemetry context (``set_span_in_context`` + ``context.attach``) for the
    whole handler. Spans the handler creates - and outbound-call spans - are
    therefore children of the server span, so the trace tree is correct (the
    default backdated mode cannot parent in-handler spans). The wrapper detaches
    the context token and ends the span in a ``finally``, so the token is always
    balanced and never leaked even when the handler raises, and it is correct
    under concurrent requests (each request attaches and detaches its own
    token). The span name and attributes are filled in from the same
    ``RequestMetrics`` record the backdated mode uses - the bridge's enrichment
    hook runs inside dispatch while the live span is current, so it updates the
    span in place. In live mode a streamed response *is* timed end to end (the
    span ends after the body drains), so streamed records are not skipped. Live
    mode is opt-in because it adds an ASGI wrapper and one context attach/detach
    per request; the default backdated mode stays zero-overhead. ``live=True``
    returns the registered enrichment hook (same idempotency, ``on_span`` and
    ``exclude_routes`` semantics as the backdated mode, except that an excluded
    route still gets a context-carrying span - only its enrichment is skipped).
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

    # Idempotency: if this app already carries a bridge hook, warn and hand back
    # the existing one rather than appending a duplicate that would double every
    # server span (see `_existing_bridge`).
    existing = _existing_bridge(app, stacklevel=2)
    if existing is not None:
        return existing

    tracer = _otel_trace.get_tracer(__name__, tracer_provider=tracer_provider)
    SpanKind = _otel_trace.SpanKind
    propagator = _W3CPropagator()

    def _emit_span(metrics: RequestMetrics) -> None:
        # Streamed bodies are sent after this hook fires, so the metrics record
        # only times response production and cannot see a mid-stream failure.
        # Backdating a span from it would mis-time the request and hide errors;
        # skip and emit nothing rather than export a misleading span.
        if metrics.streamed:
            return
        # Name from the route template, never the concrete path: an unmatched
        # request (route is None for both 404 and 405) carries an
        # attacker-controlled, high-cardinality path. The shared helper applies
        # the method-based fallback and the unrecognised-verb collapse so the
        # span name can never explode cardinality. Raw path stays out entirely.
        span_name = _span_name(metrics.route, metrics.method)
        # Anchor the window to the end captured at dispatch completion (before
        # any other hook ran); fall back to now only if a caller built the
        # metrics without it. Backdate the start by the measured duration.
        # OpenTelemetry timestamps are integer nanoseconds.
        end_time_ns = metrics.end_time_ns
        end_time = end_time_ns if end_time_ns is not None else time.time_ns()
        start_time = end_time - int(metrics.duration_ms * 1_000_000)
        # Parent the span under the inbound W3C trace context extracted from
        # the request's trace headers (so a distributed trace is continued),
        # if any; otherwise root it in a fresh empty context. Extraction
        # happens here - in the emit hook that runs on every dispatch path
        # (success, short-circuit, error) - rather than a before_request hook,
        # which an earlier short-circuiting hook could skip. Either way the
        # parent is never the ambient context active when this retroactive
        # hook fires (which would parent under unrelated same-task work).
        carrier = metrics.parent_context
        if carrier:
            # A malformed `traceparent` makes the propagator raise; a bad inbound
            # header must not abort the span this hook emits, so fall back to a
            # fresh root context and still record the request.
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

    # Tag the hook so a later `instrument_with_otel(app)` finds it and skips a
    # duplicate registration (see the idempotency guard above).
    _emit_span._veloce_otel_bridge = True  # type: ignore[attr-defined]
    app.add_instrumentation(_emit_span, exclude_routes=exclude_routes)
    return _emit_span
