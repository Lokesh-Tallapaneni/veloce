"""OpenTelemetry tracing bridge - one server span per finished HTTP request.

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
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from veloce._protocol_constants import (
    HTTP_METHOD_CONNECT,
    HTTP_METHOD_DELETE,
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
    HTTP_METHOD_PATCH,
    HTTP_METHOD_POST,
    HTTP_METHOD_PUT,
    HTTP_METHOD_TRACE,
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
    from opentelemetry import trace as _otel_trace
    from opentelemetry.context import Context as _OtelContext
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator as _W3CPropagator,
    )

    _OTEL_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without opentelemetry
    _otel_trace = None  # type: ignore[assignment]
    _OtelContext = None  # type: ignore[assignment,misc]
    _W3CPropagator = None  # type: ignore[assignment,misc]
    _OTEL_IMPORT_ERROR = exc


_INSTALL_HINT = (
    "instrument_with_otel requires OpenTelemetry, which is an optional "
    "dependency. Install it with: pip install veloceframework[otel]"
)

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


def instrument_with_otel(app: Veloce, tracer_provider: Any | None = None) -> Callable[..., Any]:
    """Bridge a Veloce app's instrumentation into OpenTelemetry spans.

    Registers a single :meth:`Veloce.add_instrumentation` hook that turns each
    finished non-streamed request into a ``SpanKind.SERVER`` span. The span name
    is the matched route template when available (``metrics.route``); when no
    route matched it falls back to a stable, low-cardinality method-based name
    (``"HTTP GET"``) and the concrete path is never used as a name or attribute.
    Each span carries ``http.request.method``, ``http.route`` (only when a route
    matched), ``http.response.status_code``, and a ``duration_ms`` attribute; a
    ``5xx`` status marks the span error.

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
    """
    if _OTEL_IMPORT_ERROR is not None:
        raise ImportError(_INSTALL_HINT) from _OTEL_IMPORT_ERROR

    tracer = _otel_trace.get_tracer(__name__, tracer_provider=tracer_provider)
    SpanKind = _otel_trace.SpanKind
    StatusCode = _otel_trace.StatusCode
    Status = _otel_trace.Status
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
        # attacker-controlled, high-cardinality path. Fall back to a stable
        # method-based name - but only for a recognised HTTP method, since the
        # method token is also attacker-controlled (Veloce accepts arbitrary
        # verbs). An unrecognised verb collapses to a single constant so the
        # span name can never explode cardinality. Raw path stays out entirely.
        if metrics.route is not None:
            span_name = metrics.route
        elif metrics.method in _STANDARD_METHODS:
            span_name = f"HTTP {metrics.method}"
        else:
            span_name = "HTTP other"
        # Anchor the window to the end captured at dispatch completion (before
        # any other hook ran); fall back to now only if a caller built the
        # metrics without it. Backdate the start by the measured duration.
        # OpenTelemetry timestamps are integer nanoseconds.
        end_time = metrics.end_time_ns if metrics.end_time_ns is not None else time.time_ns()
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
        parent = propagator.extract(cast("dict[str, str]", carrier)) if carrier else _OtelContext()
        span = tracer.start_span(
            span_name,
            context=parent,
            kind=SpanKind.SERVER,
            start_time=start_time,
        )
        try:
            span.set_attribute("http.request.method", metrics.method)
            if metrics.route is not None:
                span.set_attribute("http.route", metrics.route)
            span.set_attribute("http.response.status_code", metrics.status_code)
            span.set_attribute("duration_ms", metrics.duration_ms)
            if metrics.status_code >= HTTP_500_INTERNAL_SERVER_ERROR:
                span.set_status(Status(StatusCode.ERROR))
        finally:
            span.end(end_time=end_time)

    app.add_instrumentation(_emit_span)
    return _emit_span
