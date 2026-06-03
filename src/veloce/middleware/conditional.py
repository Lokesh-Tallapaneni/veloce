"""Conditional GET - synthesize/forward validators and emit 304s.

Honors conditional requests per RFC 9110 Sec. 13: when a buffered ``GET``
or ``HEAD`` response carries (or, optionally, is given a synthesized weak)
``ETag`` / ``Last-Modified`` validator, an ``If-None-Match`` /
``If-Modified-Since`` precondition is evaluated and a matching request is
downgraded to ``304 Not Modified`` with an empty body.
"""

from __future__ import annotations

from veloce._constants import HEADER_CACHE_CONTROL, HEADER_ETAG, HEADER_LAST_MODIFIED
from veloce.http.request import Request
from veloce.http.response import Response, StreamingResponse, header_get, header_present
from veloce.middleware.base import Middleware
from veloce.status import HTTP_200_OK


class ConditionalGetMiddleware(Middleware):
    """Emit 304 responses for satisfied GET/HEAD preconditions.

    With ``auto_etag`` (default), a weak ``ETag`` is synthesized for a
    buffered, non-empty, non-streaming 200 response that lacks one (unless
    ``Cache-Control: no-store`` is set). Register this AFTER ``GZipMiddleware``
    so a synthesized/forwarded ETag reflects the post-compression bytes;
    ``StreamingResponse`` bodies are intentionally not buffered for synthesis.

    Usage::

        app.add_middleware(GZipMiddleware())
        app.add_middleware(ConditionalGetMiddleware())
    """

    def __init__(self, *, auto_etag: bool = True) -> None:
        self.auto_etag = auto_etag

    async def process_response(self, request: Request, response: Response) -> Response:
        if request.method not in ("GET", "HEAD"):
            return response

        # Field names are case-insensitive (RFC 9110 Sec. 5.1): a handler that
        # set `etag`/`Etag` or `cache-control: no-store` in any casing must be
        # honored, so probe the plain-dict headers case-insensitively.
        existing_etag = header_present(response.headers, HEADER_ETAG)
        cache_control = header_get(response.headers, HEADER_CACHE_CONTROL) or ""
        if (
            self.auto_etag
            and not existing_etag
            and response.status_code == HTTP_200_OK
            and not isinstance(response, StreamingResponse)
            and response.body
            and "no-store" not in cache_control.lower()
        ):
            response.add_etag(weak=True)

        # `make_conditional()` clears `body` but not `_stream`, so downgrading a
        # streamed response to 304 would emit a bodiless status alongside the
        # original chunks - protocol-invalid per RFC 9110 Sec. 15.4.5 (a 304
        # carries no content). Skip the downgrade for streamed responses; they
        # pass through unchanged (the stream is never buffered for revalidation).
        if not response.is_streamed and (
            header_present(response.headers, HEADER_ETAG)
            or header_present(response.headers, HEADER_LAST_MODIFIED)
        ):
            return response.make_conditional(request)
        return response
