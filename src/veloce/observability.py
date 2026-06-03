"""Observability - structured access logging driven by RequestMetrics.

Optional helpers that turn each finished HTTP request into a log record,
sourced from the same low-cardinality :class:`RequestMetrics` record the
OpenTelemetry bridge uses, so logs and traces stay joinable on
``(route, status)``. Two factories are provided:

- :func:`log_requests_as_json` - emit one JSON record per request via a
  caller-supplied logger (no handler bootstrap).
- :func:`instrument_access_log` - the unified access log; bootstraps a
  default handler like ``LoggingMiddleware`` and supports text or JSON.

Both register a single :meth:`Veloce.add_instrumentation` hook, gate on
``logger.isEnabledFor`` so a muted access log does zero serialization
work, and use the route *template* (not the concrete path) for
aggregation safety. Register one of these instead of ``LoggingMiddleware``,
not in addition (doing both double-logs).

Usage::

    from veloce.observability import instrument_access_log

    app = Veloce()
    instrument_access_log(app, json=True)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce
    from veloce.instrumentation import RequestMetrics


def _default_access_logger() -> logging.Logger:
    """Resolve the ``veloce.access`` logger, bootstrapping a handler once."""
    logger = logging.getLogger("veloce.access")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)
    return logger


def log_requests_as_json(
    app: Veloce,
    *,
    logger: logging.Logger | None = None,
    level: int = logging.INFO,
    include_path: bool = False,
) -> Callable[[RequestMetrics], None]:
    """Register a hook emitting one JSON access-log record per request."""
    resolved = logger if logger is not None else logging.getLogger("veloce.access")
    dumps = app.json.dumps

    def _emit(metrics: RequestMetrics) -> None:
        if not resolved.isEnabledFor(level):
            return
        payload = {
            "method": metrics.method,
            "route": metrics.route,
            "status": metrics.status_code,
            "duration_ms": round(metrics.duration_ms, 3),
            "streamed": metrics.streamed,
        }
        if include_path:
            payload["path"] = metrics.path
        resolved.log(level, "%s", dumps(payload).decode())

    app.add_instrumentation(_emit)
    return _emit


def instrument_access_log(
    app: Veloce,
    *,
    logger: logging.Logger | None = None,
    json: bool = False,
    include_streamed: bool = True,
) -> Callable[[RequestMetrics], None]:
    """Register the unified access-log hook (text or JSON), route-keyed."""
    resolved = logger if logger is not None else _default_access_logger()

    def _emit(metrics: RequestMetrics) -> None:
        if not resolved.isEnabledFor(logging.INFO):
            return
        if not include_streamed and metrics.streamed:
            return
        route_label = metrics.route if metrics.route is not None else metrics.path
        if json:
            payload = {
                "method": metrics.method,
                "route": metrics.route,
                "status": metrics.status_code,
                "duration_ms": round(metrics.duration_ms, 3),
            }
            resolved.info("%s", app.json.dumps(payload).decode())
        else:
            resolved.info(
                "%s %s %d %.1fms",
                metrics.method,
                route_label,
                metrics.status_code,
                metrics.duration_ms,
            )

    app.add_instrumentation(_emit)
    return _emit
