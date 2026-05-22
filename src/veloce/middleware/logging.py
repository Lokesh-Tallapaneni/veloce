"""Request logging and request-ID middleware."""

from __future__ import annotations

import logging
import time
import uuid

from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware


class LoggingMiddleware(Middleware):
    """Structured request/response access logging."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        if logger is None:
            self.logger = logging.getLogger("veloce.access")
            if not self.logger.handlers:
                handler = logging.StreamHandler()
                handler.setFormatter(
                    logging.Formatter(
                        "%(asctime)s - %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                    )
                )
                self.logger.addHandler(handler)
                self.logger.setLevel(logging.INFO)
        else:
            self.logger = logger

    # Stash the start timestamp on the request itself rather than in a
    # middleware-owned dict keyed by id(request). A handler exception
    # used to leave the entry in the dict forever (memory leak), and
    # CPython can recycle id()s of GC'd requests for unrelated objects
    # — a future request could read a stale timestamp and log
    # nonsensical durations. Tying the start time to the request's
    # lifetime sidesteps both problems.
    _START_KEY = "__veloce_logging_start"

    async def process_request(self, request: Request) -> Response | None:
        # Skip the `time.monotonic()` call entirely when the logger is
        # not actually going to emit anything — the (typically) muted
        # access log is a common production setup, and the clock read
        # is cheap but non-zero.
        if self.logger.isEnabledFor(logging.INFO):
            request._state[self._START_KEY] = time.monotonic()
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
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
            request.method,
            request.path,
            response.status_code,
            duration_ms,
        )
        return response


class RequestIDMiddleware(Middleware):
    """Assign a unique request ID to each request and echo it in the response."""

    def __init__(self, header_name: str = "X-Request-ID") -> None:
        self.header_name = header_name

    async def process_request(self, request: Request) -> Response | None:
        request_id = request.headers.get(self.header_name.lower(), str(uuid.uuid4()))
        request._state["request_id"] = request_id
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        request_id = request._state.get("request_id")
        if request_id:
            response.headers[self.header_name] = request_id
            response._encoded = None
        return response
