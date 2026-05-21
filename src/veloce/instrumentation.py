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
    """

    __slots__ = ("method", "path", "route", "status_code", "duration_ms")

    def __init__(
        self,
        method: str,
        path: str,
        route: str | None,
        status_code: int,
        duration_ms: float,
    ) -> None:
        self.method = method
        self.path = path
        self.route = route
        self.status_code = status_code
        self.duration_ms = duration_ms

    def __repr__(self) -> str:
        return (
            f"RequestMetrics(method={self.method!r}, path={self.path!r}, "
            f"route={self.route!r}, status_code={self.status_code}, "
            f"duration_ms={self.duration_ms:.3f})"
        )
