"""Instrumentation — per-request metrics record for observability hooks.

`RequestMetrics` is the record handed to every hook registered with
`Veloce.add_instrumentation`.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Public classes ────────────────────────────────────────


@dataclass(slots=True, eq=False, repr=False)
class RequestMetrics:
    """A finished HTTP request, as seen by an instrumentation hook.

    `route` is the matched path template (`/items/{id}`), safe as a metric
    label; it is `None` for both a `404` and a `405`, so group by
    `(route, status_code)` to keep those apart. `path` is high-cardinality.

    `streamed` marks a response whose body is a streaming iterator: the hook
    fires before the body is emitted, so `duration_ms` and `status_code`
    exclude draining the stream and any mid-stream failure.

    `end_time_ns` is the `time.time_ns()` instant dispatch returned, captured
    before any hook runs, so a slow earlier hook cannot shift a span past the
    real request boundary.

    `error_type` is `type(exc).__qualname__` for an *unhandled* exception that
    became a `5xx`, and `None` for every other outcome - including a `5xx`
    returned deliberately without a raised exception. The class name only is
    carried, never the message, which may hold sensitive text.
    """

    method: str
    path: str
    route: str | None
    status_code: int
    duration_ms: float
    streamed: bool = False
    end_time_ns: int | None = None
    error_type: str | None = None
    # Inbound trace headers as a carrier dict; the core never interprets it.
    parent_context: object | None = None

    def __repr__(self) -> str:
        return (
            f"RequestMetrics(method={self.method!r}, path={self.path!r}, "
            f"route={self.route!r}, status_code={self.status_code}, "
            f"duration_ms={self.duration_ms:.3f}, streamed={self.streamed}, "
            f"error_type={self.error_type!r})"
        )
