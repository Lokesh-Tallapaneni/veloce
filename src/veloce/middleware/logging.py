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

    # Stash the start timestamp on the request itself rather than in a
    # middleware-owned dict keyed by id(request). Such a dict leaks an entry
    # for every request whose handler raises, and CPython recycles the id() of
    # a collected request for unrelated objects - so a later request reads a
    # stale timestamp and logs a nonsensical duration. Tying the start time to
    # the request's own lifetime sidesteps both.
    _START_KEY = "__veloce_logging_start"

    def __init__(self, logger: logging.Logger | None = None, *, name: str | None = None) -> None:
        super().__init__(name=name)
        if logger is None:
            # "veloce.access" is part of the public contract: applications and
            # the test suite attach handlers to that exact name, so it stays a
            # literal rather than `__name__`.
            self.logger = logging.getLogger("veloce.access")
            # Handlers and level are bootstrapped independently. We check
            # `self.logger.handlers` (direct list) rather than
            # `hasHandlers()` (walks parents): a root-level handler
            # configured at WARNING would silently swallow our INFO
            # access-log records, so we always want our own handler when
            # the access logger has none of its own.
            if not self.logger.handlers:
                handler = logging.StreamHandler()
                handler.setFormatter(
                    logging.Formatter(
                        "%(asctime)s - %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                    )
                )
                self.logger.addHandler(handler)
            # Level is orthogonal to handlers: a defensive `NullHandler`
            # pre-installed at import time would suppress the level
            # bootstrap if it were coupled to the handler check, leaving
            # the logger at NOTSET (== inherits root, typically WARNING)
            # and silencing access logs. Only set INFO when the level is
            # genuinely unconfigured.
            if self.logger.level == logging.NOTSET:
                self.logger.setLevel(logging.INFO)
        else:
            self.logger = logger

    async def process_request(self, request: Request) -> Response | None:
        """Record the request start time for duration logging."""
        # Skip the `time.monotonic()` call entirely when the logger is
        # not actually going to emit anything - the (typically) muted
        # access log is a common production setup, and the clock read
        # is cheap but non-zero.
        if self.logger.isEnabledFor(logging.INFO):
            request._state[self._START_KEY] = time.monotonic()
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Log the request method, path, status, and timing."""
        if not self.logger.isEnabledFor(logging.INFO):
            return response
        # `pop` rather than `get` so a downstream second-pass through
        # this middleware on the same request (rewriting via
        # `make_response`, after_request hooks, etc.) does not read a
        # stale start time and report the wrong duration.
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
        # An inbound id is reflected into the response header, so a malformed
        # value (CR/LF/NUL) would fail header emission; mint a fresh id instead
        # of trusting attacker-controlled bytes.
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
