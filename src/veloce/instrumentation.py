"""Observability instrumentation primitives.

`RequestMetrics` is the per-request record handed to every instrumentation
hook registered with `Veloce.add_instrumentation`. It carries exactly the
low-cardinality dimensions an observability backend wants — the route
*template* (not the concrete path), the method, the status, and the
wall-clock duration — so a metrics exporter or tracing bridge can record a
request without re-deriving anything.
"""

from __future__ import annotations


class RequestMetrics:
    """A finished HTTP request, as seen by an instrumentation hook.

    `route` is the matched route's path template (`/items/{id}`), which is
    safe to use as a metric label; it is `None` whenever no route+method
    pair matched — both a `404` (no such path) and a `405` (the path
    exists but the method is not allowed). Group by `(route, status_code)`
    to keep those apart. `path` is the concrete request path and is
    high-cardinality — prefer `route` for aggregation.

    `streamed` is `True` when the response body is a streaming iterator
    (`StreamingResponse`, `EventSourceResponse`, a large `FileResponse`).
    For those, the hook fires *before* the body is emitted on the ASGI send
    path, so `duration_ms` and `status_code` reflect only the time to
    produce the response object — not the time to drain the stream, and not
    a failure that happens mid-stream. A tracing bridge that needs accurate
    end-of-request timing should skip records with `streamed` set.

    `end_time_ns` is the wall-clock (`time.time_ns()`) instant the request
    finished, captured the moment dispatch returned — *before* any
    instrumentation hook or `request_finished` receiver runs. A tracing
    bridge should anchor its span window to this value (and `duration_ms`)
    rather than reading the clock when its own hook executes, so a slow
    earlier hook cannot shift the span past the real request boundary.
    """

    __slots__ = (
        "method",
        "path",
        "route",
        "status_code",
        "duration_ms",
        "streamed",
        "end_time_ns",
        "parent_context",
    )

    def __init__(
        self,
        method: str,
        path: str,
        route: str | None,
        status_code: int,
        duration_ms: float,
        streamed: bool = False,
        end_time_ns: int | None = None,
        parent_context: object | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.route = route
        self.status_code = status_code
        self.duration_ms = duration_ms
        self.streamed = streamed
        self.end_time_ns = end_time_ns
        # Inbound distributed-trace headers (a `{"traceparent": ..., maybe
        # "tracestate": ...}` carrier dict) so a tracing bridge (e.g.
        # veloce.otel) can extract a parent context and continue the trace.
        # `None` when the request carried no trace headers. The core never
        # interprets this value — it stays framework-agnostic.
        self.parent_context = parent_context

    def __repr__(self) -> str:
        return (
            f"RequestMetrics(method={self.method!r}, path={self.path!r}, "
            f"route={self.route!r}, status_code={self.status_code}, "
            f"duration_ms={self.duration_ms:.3f}, streamed={self.streamed})"
        )
