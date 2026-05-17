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
        self._request_times: dict[int, float] = {}

    async def process_request(self, request: Request) -> Response | None:
        self._request_times[id(request)] = time.monotonic()
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        start = self._request_times.pop(id(request), None)
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
