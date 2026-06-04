"""Prometheus metrics exporter - request counters and a duration histogram.

This is an **optional** integration that turns each finished HTTP request into
Prometheus time series using Veloce's existing instrumentation hook
(:meth:`Veloce.add_instrumentation`). It does not replace or rebuild that hook
- it registers exactly one hook that records metrics per request.

It builds on :class:`~veloce.instrumentation.RequestMetrics`, the same
low-cardinality record the OpenTelemetry bridge consumes. Series are labelled
by the matched route *template* (``metrics.route``, e.g. ``/items/{id}``) rather
than the concrete path, so an attacker-controlled URL can never explode label
cardinality; an unmatched request (a ``404`` for an unknown path or a ``405``
for a disallowed method, where ``metrics.route`` is ``None``) collapses to a
single constant ``"<unmatched>"`` label.

Only ``prometheus_client`` is required, and it is optional: importing this
module (or ``import veloce``) never requires it. The import is guarded and
:func:`instrument_with_prometheus` raises a clear :class:`ImportError` with an
install hint when it is missing. Install the extra with::

    pip install veloceframework[metrics]

Then wire it up once at startup and expose the metrics yourself (the exporter
only records series; serving ``/metrics`` is the application's job)::

    from veloce import Veloce
    from veloce.metrics import instrument_with_prometheus

    app = Veloce()
    instrument_with_prometheus(app)

The exporter is **registry-agnostic**: pass your own
:class:`~prometheus_client.CollectorRegistry` to isolate apps (or to use a
multiprocess registry); otherwise the default global registry is used. Because
the metrics are built per call and bound to the chosen registry, instrumenting
two apps against two distinct registries does not raise the "Duplicated
timeseries" error a shared global collector would.

**Scope.** This first cut exports a request **counter** and a request-duration
**histogram**. It does not expose an in-progress (in-flight concurrency) gauge:
:class:`RequestMetrics` fires only at request *finish*, so a single finish-only
hook cannot do the increment-before / decrement-after a live gauge needs. That
belongs to a paired request-lifecycle hook and is intentionally out of scope
here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce
    from veloce.instrumentation import RequestMetrics

# prometheus_client is optional. Import it at module load so the factory below
# can build collectors, but tolerate its absence: a box without the extra must
# still be able to `import veloce` and `import veloce.metrics`. When unavailable
# we record the sentinel error and gate the real requirement behind
# `instrument_with_prometheus`, where a precise ImportError with an install hint
# is raised.
try:
    import prometheus_client as _prom
    from prometheus_client import Counter, Histogram

    _PROM_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without prometheus_client
    _prom = None  # type: ignore[assignment]
    Counter = None  # type: ignore[assignment,misc]
    Histogram = None  # type: ignore[assignment,misc]
    _PROM_IMPORT_ERROR = exc


_INSTALL_HINT = (
    "instrument_with_prometheus requires prometheus_client, which is an optional "
    "dependency. Install it with: pip install veloceframework[metrics]"
)

# prometheus_client's own conventional default histogram buckets (seconds).
# Public and stable - used when the caller does not pass custom buckets.
_DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
)

# Stable, low-cardinality label used when no route matched (404/405). The
# concrete request path is attacker-controlled and high-cardinality, so it is
# never used as a label value.
_UNMATCHED_ROUTE = "<unmatched>"


def instrument_with_prometheus(
    app: Veloce,
    *,
    registry: Any | None = None,
    prefix: str = "http",
    buckets: Sequence[float] | None = None,
    group_status: bool = True,
    exclude_routes: Iterable[str] | None = None,
) -> Callable[[RequestMetrics], None]:
    """Export Prometheus request metrics from a Veloce app's instrumentation.

    Register a single :meth:`Veloce.add_instrumentation` hook that records, for
    each finished request, a ``{prefix}_requests_total`` counter (labelled by
    method, route template, and status) and a
    ``{prefix}_request_duration_seconds`` histogram (labelled by method and
    route template). The route label is always the matched route *template*; an
    unmatched request (``404``/``405``) uses the constant ``"<unmatched>"``
    label, so an attacker-controlled path can never explode cardinality.

    Bind the metrics to ``registry`` when given, else to prometheus_client's
    default registry. Because the collectors are built here and bound to the
    chosen registry, two apps instrumented against two distinct registries do
    not collide; instrumenting twice against the *same* registry raises
    prometheus_client's "Duplicated timeseries" error, so use a fresh registry
    per app when isolating.

    ``prefix`` names the metrics (``http`` -> ``http_requests_total``);
    ``buckets`` overrides the histogram's second-valued buckets, defaulting to
    prometheus_client's conventional set. ``exclude_routes`` (a set of matched
    route *templates*, e.g. ``{"/health", "/metrics"}``) suppresses these series
    for noisy routes via Veloce's core instrumentation filter, so health checks
    and the scrape endpoint itself never inflate request counts.
    ``group_status`` (default ``True``)
    collapses the status code into its class bucket - ``200`` -> ``"2xx"``,
    ``404`` -> ``"4xx"``, ``503`` -> ``"5xx"`` - as the ``status`` label, so the
    counter holds at most one series per status class; set it ``False`` to keep
    the concrete code (it is itself bounded, so that stays safe too).

    Raise :class:`ImportError` with an install hint when ``prometheus_client``
    is not installed. Return the registered hook (the value
    :meth:`Veloce.add_instrumentation` returns), so callers can hold a reference
    to it for tests or introspection.
    """
    if _PROM_IMPORT_ERROR is not None:
        raise ImportError(_INSTALL_HINT) from _PROM_IMPORT_ERROR

    registry = registry if registry is not None else _prom.REGISTRY

    requests_total = Counter(
        f"{prefix}_requests_total",
        "Total HTTP requests",
        labelnames=("method", "route", "status"),
        registry=registry,
    )
    request_duration = Histogram(
        f"{prefix}_request_duration_seconds",
        "HTTP request duration in seconds",
        labelnames=("method", "route"),
        buckets=tuple(buckets) if buckets is not None else _DEFAULT_BUCKETS,
        registry=registry,
    )

    def _record(metrics: RequestMetrics) -> None:
        # Always label by the route template, never the concrete path: an
        # unmatched request (route is None for both 404 and 405) carries an
        # attacker-controlled, high-cardinality path. Collapse it to a single
        # constant so label cardinality stays bounded.
        route_label = metrics.route if metrics.route is not None else _UNMATCHED_ROUTE
        # `group_status` collapses the concrete code into its class bucket
        # (200 -> "2xx", 404 -> "4xx") so a noisy app emits at most five status
        # series instead of one per distinct code; when False the concrete code
        # is kept (it is already bounded).
        if group_status:
            status_label = f"{metrics.status_code // 100}xx"
        else:
            status_label = str(metrics.status_code)
        requests_total.labels(metrics.method, route_label, status_label).inc()
        request_duration.labels(metrics.method, route_label).observe(metrics.duration_ms / 1000.0)

    app.add_instrumentation(_record, exclude_routes=exclude_routes)
    return _record
