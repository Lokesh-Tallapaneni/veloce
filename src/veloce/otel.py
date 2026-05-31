"""OpenTelemetry tracing bridge for a Veloce application.

This is an **optional** integration that turns each finished HTTP request into
an OpenTelemetry server span using Veloce's existing instrumentation hook
(:meth:`Veloce.add_instrumentation`). It does not replace or rebuild that hook
— it registers exactly one hook that emits a span per request.

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

**Timing limitation (honest):** Veloce's instrumentation hook fires *after* the
response is produced — :meth:`Veloce._run_instrumentation` runs once the request
is finished, with the already-measured wall-clock duration. The span emitted
here therefore *represents* a request (its method, route, status, and duration
attribute) but is **not** a live wrap around handler execution; it is recorded
retroactively from the :class:`~veloce.instrumentation.RequestMetrics` record.
For per-request causal context propagation you would integrate at the ASGI
layer instead; this bridge is for backend-agnostic request spans driven off the
same low-cardinality dimensions a metrics exporter consumes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

    _OTEL_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without opentelemetry
    _otel_trace = None  # type: ignore[assignment]
    _OTEL_IMPORT_ERROR = exc


_INSTALL_HINT = (
    "instrument_with_otel requires OpenTelemetry, which is an optional "
    "dependency. Install it with: pip install veloceframework[otel]"
)


def instrument_with_otel(app: Veloce, tracer_provider: Any | None = None) -> Callable[..., Any]:
    """Bridge a Veloce app's instrumentation into OpenTelemetry spans.

    Registers a single :meth:`Veloce.add_instrumentation` hook that turns each
    finished request into a ``SpanKind.SERVER`` span. The span name is the
    matched route template when available (``metrics.route``), falling back to
    the concrete path (``metrics.path``) for unmatched requests. Each span
    carries ``http.request.method``, ``http.route``, ``http.response.status_code``,
    and a ``duration_ms`` attribute; a ``5xx`` status marks the span error.

    Pass ``tracer_provider`` to source the tracer from a specific provider;
    otherwise the globally configured provider is used. The application owns SDK
    and exporter configuration — this only emits spans.

    Raises :class:`ImportError` with an install hint when OpenTelemetry is not
    installed. Returns the registered hook (which is also the value
    :meth:`Veloce.add_instrumentation` returns), so callers can hold a reference
    to it for tests or introspection.

    Note the timing limitation documented in this module: the span is recorded
    retroactively from the request's metrics record, not as a live wrap of
    handler execution.
    """
    if _OTEL_IMPORT_ERROR is not None:
        raise ImportError(_INSTALL_HINT) from _OTEL_IMPORT_ERROR

    tracer = _otel_trace.get_tracer(__name__, tracer_provider=tracer_provider)
    SpanKind = _otel_trace.SpanKind
    StatusCode = _otel_trace.StatusCode
    Status = _otel_trace.Status

    def _emit_span(metrics: RequestMetrics) -> None:
        span_name = metrics.route or metrics.path
        span = tracer.start_span(span_name, kind=SpanKind.SERVER)
        try:
            span.set_attribute("http.request.method", metrics.method)
            if metrics.route is not None:
                span.set_attribute("http.route", metrics.route)
            span.set_attribute("http.response.status_code", metrics.status_code)
            span.set_attribute("duration_ms", metrics.duration_ms)
            if metrics.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR))
        finally:
            span.end()

    app.add_instrumentation(_emit_span)
    return _emit_span
