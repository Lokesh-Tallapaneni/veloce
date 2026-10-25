"""Prometheus metrics exporter — request counters and a duration histogram.

An optional integration registering one :meth:`Veloce.add_instrumentation` hook
that turns each finished request into Prometheus time series. Series are labelled
by the matched route *template*, never the concrete path, so an
attacker-controlled URL can never explode label cardinality; an unmatched
request collapses to a single constant ``"<unmatched>"`` label.

``prometheus_client`` is required only here; importing this module never requires
it. Install the extra with::

    pip install veloceframework[metrics]

Then wire it up once at startup and serve ``/metrics`` yourself::

    from veloce import Veloce
    from veloce.metrics import instrument_with_prometheus

    app = Veloce()
    instrument_with_prometheus(app)

Pass your own :class:`~prometheus_client.CollectorRegistry` to isolate apps.
Not re-exported from the top-level ``veloce`` package: importing it costs ~72 ms
on top of ``import veloce`` (measured with ``python -X importtime``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce
    from veloce.instrumentation import RequestMetrics

# Optional: absence is recorded and gated behind `instrument_with_prometheus`.
try:
    import prometheus_client as _prom
    from prometheus_client import Counter, Histogram

    _PROM_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without prometheus_client
    # `Any` rather than three suppressions: `_PROM_IMPORT_ERROR` gates every use.
    _prom = cast("Any", None)
    Counter = Histogram = cast("Any", None)  # type: ignore[misc]
    _PROM_IMPORT_ERROR = exc


_INSTALL_HINT = (
    "instrument_with_prometheus requires prometheus_client, which is an optional "
    "dependency. Install it with: pip install veloceframework[metrics]"
)

# prometheus_client's own conventional default histogram buckets (seconds).
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

# For 404/405: the concrete path is attacker-controlled, so never a label value.
_UNMATCHED_ROUTE = "<unmatched>"


def _rewrite_duplicate(err: ValueError, prefix: str, is_global: bool) -> ValueError:
    """Restate a collector-name collision as something the caller can act on.

    Any other `ValueError` is returned untouched.
    """
    if "Duplicated" not in str(err):
        return err
    where = (
        "prometheus_client's process-global registry (the default, so that a "
        "second app or a test that rebuilds the app collides)"
        if is_global
        else "the registry passed as `registry=`"
    )
    return ValueError(
        f"metrics named '{prefix}_*' are already registered on {where}. "
        f"Pass a fresh registry per app - `instrument_with_prometheus(app, "
        f"registry=CollectorRegistry())` - or a distinct `prefix=`. "
        f"Original error: {err}"
    )


def instrument_with_prometheus(
    app: Veloce,
    *,
    registry: Any = None,
    prefix: str = "http",
    buckets: Sequence[float] | None = None,
    group_status: bool = True,
    exclude_routes: Iterable[str] | None = None,
) -> Callable[[RequestMetrics], None]:
    """Export Prometheus request metrics from a Veloce app's instrumentation.

    Register one :meth:`Veloce.add_instrumentation` hook that records, per
    finished request, a ``{prefix}_requests_total`` counter (method, route
    template, status) and a ``{prefix}_request_duration_seconds`` histogram
    (method, route template). An unmatched request uses the constant
    ``"<unmatched>"`` route label.

    Bind the metrics to ``registry`` when given, else to prometheus_client's
    default; instrumenting twice against the same registry raises its
    "Duplicated timeseries" error, so use a fresh registry per app to isolate.
    ``buckets`` overrides the histogram's second-valued buckets.
    ``exclude_routes`` is a set of route *templates* whose series are suppressed.
    ``group_status`` (default ``True``) collapses the status code into its class
    bucket - ``200`` -> ``"2xx"`` - as the ``status`` label.

    Raise :class:`ImportError` with an install hint when ``prometheus_client``
    is not installed. Return the registered hook.
    """
    if _PROM_IMPORT_ERROR is not None:
        raise ImportError(_INSTALL_HINT) from _PROM_IMPORT_ERROR

    registry = registry if registry is not None else _prom.REGISTRY

    try:
        requests_total = Counter(
            f"{prefix}_requests_total",
            "Total HTTP requests",
            labelnames=("method", "route", "status"),
            registry=registry,
        )
    except ValueError as err:
        raise _rewrite_duplicate(err, prefix, registry is _prom.REGISTRY) from err
    try:
        request_duration = Histogram(
            f"{prefix}_request_duration_seconds",
            "HTTP request duration in seconds",
            labelnames=("method", "route"),
            buckets=tuple(buckets) if buckets is not None else _DEFAULT_BUCKETS,
            registry=registry,
        )
    except ValueError as err:
        # Drop the already-registered counter rather than leave half the series.
        registry.unregister(requests_total)
        raise _rewrite_duplicate(err, prefix, registry is _prom.REGISTRY) from err

    def _record(metrics: RequestMetrics) -> None:
        # Never the concrete path: an unmatched request's is attacker-controlled.
        route_label = metrics.route if metrics.route is not None else _UNMATCHED_ROUTE
        # A class bucket caps a noisy app at five status series; the code is bounded.
        if group_status:
            status_label = f"{metrics.status_code // 100}xx"
        else:
            status_label = str(metrics.status_code)
        requests_total.labels(metrics.method, route_label, status_label).inc()
        request_duration.labels(metrics.method, route_label).observe(metrics.duration_ms / 1000.0)

    app.add_instrumentation(_record, exclude_routes=exclude_routes)
    return _record
