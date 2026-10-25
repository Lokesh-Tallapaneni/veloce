"""Logging middleware — request/response access logging and request IDs."""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING

from veloce._constants import HEADER_X_REQUEST_ID
from veloce._internal import _LOG_SANITIZE, _header_value_has_crlf
from veloce.middleware.base import Middleware

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.request import Request
    from veloce.http.response import Response


class LoggingMiddleware(Middleware):
    """Structured request/response access logging.

    Usage::

        app.add_middleware(LoggingMiddleware())
    """

    # On the request itself, not a dict keyed by `id(request)`: that leaks an
    # entry per raising handler, and CPython recycles a collected id.
    _START_KEY = "__veloce_logging_start"

    def __init__(self, logger: logging.Logger | None = None, *, name: str | None = None) -> None:
        super().__init__(name=name)
        if logger is None:
            # A public contract: applications attach handlers to this name.
            self.logger = logging.getLogger("veloce.access")
            # The direct list, not `hasHandlers()`: a root handler at WARNING
            # would silently swallow these INFO records.
            if not self.logger.handlers:
                handler = logging.StreamHandler()
                handler.setFormatter(
                    logging.Formatter(
                        "%(asctime)s - %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                    )
                )
                self.logger.addHandler(handler)
            # Separate from the handler check, which a `NullHandler` would pass.
            if self.logger.level == logging.NOTSET:
                self.logger.setLevel(logging.INFO)
        else:
            self.logger = logger

    async def process_request(self, request: Request) -> Response | None:
        """Record the request start time for duration logging."""
        # A muted access log is common, and the clock read is not free.
        if self.logger.isEnabledFor(logging.INFO):
            request._state[self._START_KEY] = time.monotonic()
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Log the request method, path, status, and timing."""
        if not self.logger.isEnabledFor(logging.INFO):
            return response
        # `pop`, so a second pass cannot read a stale start time.
        start = request._state.pop(self._START_KEY, None)
        duration_ms = (time.monotonic() - start) * 1000 if start else 0
        self.logger.info(
            "%s %s %d %.1fms",
            request.method.translate(_LOG_SANITIZE),
            request.path.translate(_LOG_SANITIZE),
            response.status_code,
            duration_ms,
        )
        return response


class RequestIDMiddleware(Middleware):
    """Assign a unique request ID to each request and echo it in the response.

    Usage::

        app.add_middleware(RequestIDMiddleware())
    """

    def __init__(self, header_name: str = HEADER_X_REQUEST_ID, *, name: str | None = None) -> None:
        super().__init__(name=name)
        self.header_name = header_name
        self._header_name_lower = header_name.lower()

    async def process_request(self, request: Request) -> Response | None:
        """Attach a unique request ID to each request."""
        # Reflected into the response header, so a CR/LF/NUL value is replaced.
        inbound = request.headers.get(self._header_name_lower)
        if inbound and not _header_value_has_crlf(inbound):
            request_id = inbound
        else:
            request_id = str(uuid.uuid4())
        request._state["request_id"] = request_id
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Echo the request ID in the response headers."""
        request_id = request._state.get("request_id")
        if request_id:
            response.headers[self.header_name] = request_id
            response._encoded = None
        return response
