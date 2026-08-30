"""Observability — structured access logging driven by RequestMetrics.

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
aggregation safety. An unmatched request (a 404/405 carrying no route
template) falls back to the concrete request path in the text log,
sanitized of control characters so it cannot forge a log line. Register
one of these instead of ``LoggingMiddleware``, not in addition (doing
both double-logs).

Usage::

    from veloce.observability import instrument_access_log

    app = Veloce()
    instrument_access_log(app, json=True)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from veloce._internal import _LOG_SANITIZE

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce
    from veloce.instrumentation import RequestMetrics


def _json_payload(
    metrics: RequestMetrics,
    *,
    include_streamed: bool,
    include_path: bool,
) -> dict[str, object]:
    """Build the JSON access-log payload shared by both factories.

    Both emit the same low-cardinality base record (method, route template,
    status, rounded duration); the two factories differ only in whether they
    also carry `streamed` and the high-cardinality concrete `path`, so those
    are gated by flags rather than duplicating the dict construction.
    """
    payload: dict[str, object] = {
        "method": metrics.method,
        "route": metrics.route,
        "status": metrics.status_code,
        "duration_ms": round(metrics.duration_ms, 3),
    }
    if include_streamed:
        payload["streamed"] = metrics.streamed
    if include_path:
        payload["path"] = metrics.path
    return payload


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


def _register_access_log(
    app: Veloce, emit: Callable[[RequestMetrics], None]
) -> Callable[[RequestMetrics], None]:
    """Mark `emit` as the access log, register it on `app`, and hand it back.

    The marker `run(access_log=True)` reads to decide whether an access log
    is already installed. Marked rather than identified by `__module__`, so
    that a user's own access-log instrumentation can set it too and suppress
    the built-in one - testing where the function was *defined* left both
    installed, and every request logged twice.
    """
    emit.is_access_log = True  # type: ignore[attr-defined]
    app.add_instrumentation(emit)
    return emit


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
        payload = _json_payload(metrics, include_streamed=True, include_path=include_path)
        resolved.log(level, "%s", dumps(payload).decode())

    return _register_access_log(app, _emit)


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
        # Unmatched requests carry no route template; fall back to the concrete
        # path, sanitized so a CR/LF in an attacker-controlled URL cannot forge
        # or split a text log line (CWE-117).
        route_label = (
            metrics.route if metrics.route is not None else metrics.path.translate(_LOG_SANITIZE)
        )
        if json:
            payload = _json_payload(metrics, include_streamed=False, include_path=False)
            resolved.info("%s", app.json.dumps(payload).decode())
        else:
            resolved.info(
                "%s %s %d %.1fms",
                metrics.method,
                route_label,
                metrics.status_code,
                metrics.duration_ms,
            )

    return _register_access_log(app, _emit)
