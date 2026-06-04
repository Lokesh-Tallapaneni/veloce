"""Instrumentation - per-request metrics record for observability hooks.

`RequestMetrics` is the per-request record handed to every instrumentation
hook registered with `Veloce.add_instrumentation`. It carries exactly the
low-cardinality dimensions an observability backend wants - the route
*template* (not the concrete path), the method, the status, and the
wall-clock duration - so a metrics exporter or tracing bridge can record a
request without re-deriving anything.
"""

from __future__ import annotations

from dataclasses import dataclass

# -- Public classes -------------------------------------------------


@dataclass(slots=True, eq=False, repr=False)
class RequestMetrics:
    """A finished HTTP request, as seen by an instrumentation hook.

    `route` is the matched route's path template (`/items/{id}`), which is
    safe to use as a metric label; it is `None` whenever no route+method
    pair matched - both a `404` (no such path) and a `405` (the path
    exists but the method is not allowed). Group by `(route, status_code)`
    to keep those apart. `path` is the concrete request path and is
    high-cardinality - prefer `route` for aggregation.

    `streamed` is `True` when the response body is a streaming iterator
    (`StreamingResponse`, `EventSourceResponse`, a large `FileResponse`).
    For those, the hook fires *before* the body is emitted on the ASGI send
    path, so `duration_ms` and `status_code` reflect only the time to
    produce the response object - not the time to drain the stream, and not
    a failure that happens mid-stream. A tracing bridge that needs accurate
    end-of-request timing should skip records with `streamed` set.

    `end_time_ns` is the wall-clock (`time.time_ns()`) instant the request
    finished, captured the moment dispatch returned - *before* any
    instrumentation hook or `request_finished` receiver runs. A tracing
    bridge should anchor its span window to this value (and `duration_ms`)
    rather than reading the clock when its own hook executes, so a slow
    earlier hook cannot shift the span past the real request boundary.

    `error_type` is the low-cardinality class name (`type(exc).__qualname__`)
    of the exception that produced a `5xx`, set only when an *unhandled*
    raised exception turned into a server error (the debug traceback page,
    the generic `500` response, or a propagated exception). It is `None` for
    every other outcome - a `2xx`/`3xx`/`4xx`, or a `5xx` deliberately
    returned by a handler/exception handler without a raised exception. A
    tracing bridge can record it as the OpenTelemetry `error.type` attribute
    without capturing the full traceback or exception instance, keeping the
    record allocation-light. The class *name* only is carried, never the
    message (which may hold attacker-controlled or sensitive text).
    """

    method: str
    path: str
    route: str | None
    status_code: int
    duration_ms: float
    streamed: bool = False
    end_time_ns: int | None = None
    error_type: str | None = None
    # Inbound distributed-trace headers (a `{"traceparent": ..., maybe
    # "tracestate": ...}` carrier dict) so a tracing bridge (e.g.
    # veloce.otel) can extract a parent context and continue the trace.
    # `None` when the request carried no trace headers. The core never
    # interprets this value - it stays framework-agnostic.
    parent_context: object | None = None

    def __repr__(self) -> str:
        return (
            f"RequestMetrics(method={self.method!r}, path={self.path!r}, "
            f"route={self.route!r}, status_code={self.status_code}, "
            f"duration_ms={self.duration_ms:.3f}, streamed={self.streamed}, "
            f"error_type={self.error_type!r})"
        )
