"""Request refusal for `HttpProtocol` — error responses and the 413/100-continue paths."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import unquote

import httptools

import veloce.status as status
from veloce._constants import (
    MIME_JSON,
)
from veloce._internal import dumps_for
from veloce._protocol_constants import (
    HTTP_METHOD_HEAD,
)
from veloce.http._body import body_too_large, too_large_payload
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.serving.protocol._host import ProtocolHost

if TYPE_CHECKING:  # pragma: no cover
    from veloce._pipeline import CompiledPipeline
from veloce.serving.protocol._constants import _PERCENT_BYTE, CONTENT_TYPE_JSON_LINE
from veloce.serving.protocol._helpers import _strip_response_body

_logger = logging.getLogger(__name__)


class _RefusalMixin(ProtocolHost):
    """Error and refusal responses, composed into `HttpProtocol`."""

    __slots__ = ()

    def _emit_http_error(
        self, status_code: int, reason: bytes, body: bytes = b"", extra: bytes = b""
    ) -> None:
        """Write a minimal `Connection: close` HTTP/1.1 error response, then close.

        The single framing path for every pre-dispatch refusal, owning only the
        wire framing: the `_oversized` latch, source EOF and timer resets stay
        with the callers.
        """
        transport = self.transport
        if transport is None or transport.is_closing():
            return
        phrase = reason.decode("ascii")
        head = (
            f"HTTP/1.1 {status_code} {phrase}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n"
        ).encode("ascii")
        transport.write(head + extra + b"\r\n" + body)
        transport.close()

    def _reject_oversized(self, status_code: int, reason: bytes) -> None:
        """Emit a minimal HTTP/1.1 error response and close the connection.

        For a request line or header block past its cap: the parser cannot be
        trusted to resynchronise, so the connection is terminated.
        """
        self._oversized = True
        self._emit_http_error(status_code, reason)

    def _parse_request_target(self) -> tuple[str, bytes]:
        """Split the parsed request line into `(path, raw_query_bytes)`.

        `path` is percent-decoded as the ASGI scope's is, so one app binds
        `/items/a%20b` the same way however it is served. A target with no `%`
        skips the decode, so the common path pays one C-level scan and no
        allocation. The query stays raw for the WebSocket scope.
        """
        parsed = httptools.parse_url(self.url)
        raw_path = parsed.path
        if not raw_path:
            return "/", parsed.query or b""
        if _PERCENT_BYTE in raw_path:
            # A malformed escape is left as written, as ASGI's `path` holds it.
            return unquote(raw_path.decode("ascii")), parsed.query or b""
        return raw_path.decode("ascii"), parsed.query or b""

    def _declared_content_length(self) -> int | None:
        """Parse the just-parsed request's `Content-Length` header, or None.

        None when absent or malformed, in which case the streamed
        running-total cap is the backstop.
        """
        raw = self._raw_content_length
        if raw is None:
            return None
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None

    def _wants_continue(self) -> bool:
        """Return whether the just-parsed request asks for a 100 Continue.

        RFC 9110 Sec. 10.1.1 forbids the interim response to an HTTP/1.0
        client, so the version is checked first. Older httptools builds may not
        expose `get_http_version`; its absence means "do not send".
        """
        try:
            if self.parser.get_http_version() != "1.1":
                return False
        except (AttributeError, RuntimeError):
            return False
        return self._has_expect_continue

    def _refusal_request(self) -> Request | None:
        """Build a body-less `Request` for a pre-dispatch refusal, or `None`.

        The request line and headers are parsed by the time a declared
        `Content-Length` is checked, so the refusal carries the request the
        response phase would have seen. No body source: that is what is refused.
        """
        transport = self.transport
        if transport is None or transport.is_closing():
            return None
        try:
            path, query_bytes = self._parse_request_target()
            return Request(
                method=self.parser.get_method().decode("ascii"),
                path=path,
                query_string=query_bytes.decode("ascii"),
                headers=list(self.headers),
                body=b"",
                transport=transport,
            )
        except Exception:  # noqa: BLE001 - a refused upgrade falls back to bare framing
            # A refusal never goes unsent: the caller falls back to bare framing.
            return None

    def _reject_413(self, request: Request | None = None) -> None:
        """Emit a 413 and close - the body exceeds MAX_CONTENT_LENGTH.

        The connection is terminated rather than trusted to resynchronise after
        a partially-read over-limit body. With a `request` the refusal runs
        through the app's response phase, so it carries the CORS and security
        headers a served response would; without one the bare framing is written.
        """
        self._oversized = True
        if self._current_source is not None:
            self._current_source.feed_eof()
            self._current_source = None
        max_size = self.app.config.get("MAX_CONTENT_LENGTH")
        if request is not None:
            cp = self.app._ensure_pipeline()
            # A probe: it separates the refusal written below from one that awaits.
            if (cp is not None and cp.http_post is not None) or self.app._find_error_handler(
                request, body_too_large(max_size)
            ) is not None:
                # A task because the handler may await and this is a sync callback.
                task = self.loop.create_task(self._emit_refusal(request, cp, max_size))
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
                return
        # RFC 9110 Sec. 15.5.14's phrase; the status table spells the pre-9110 one.
        self._emit_http_error(
            status.HTTP_413_CONTENT_TOO_LARGE,
            b"Content Too Large",
            dumps_for(self.app, too_large_payload(max_size)),
            extra=CONTENT_TYPE_JSON_LINE,
        )

    async def _emit_refusal(
        self, request: Request, cp: CompiledPipeline | None, max_size: int | None
    ) -> None:
        """Build the 413 the way the ASGI path builds it, then frame it and close.

        Through the app's own builder, so a registered `RequestEntityTooLarge`
        handler shapes this refusal exactly as it shapes the ASGI one.
        """
        try:
            response = await self.app._body_too_large_response(request, cp, max_size)
        except Exception:
            # A failing handler must not swallow the refusal.
            _logger.exception("building a body-too-large refusal raised")
            response = Response(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                body=dumps_for(self.app, too_large_payload(max_size)),
                content_type=MIME_JSON,
            )
        transport = self.transport
        if transport is None or transport.is_closing():
            return
        try:
            if request.method == HTTP_METHOD_HEAD:
                # RFC 9110 Sec. 9.3.2: no body whatever the framing says.
                transport.write(_strip_response_body(response.encode(keep_alive=False)))
            elif response.is_streamed:
                await response.stream_to(transport, drain=self.drain, keep_alive=False)
            else:
                transport.write(response.encode(keep_alive=False))
        except Exception:
            # Logged here because this task is never awaited.
            _logger.exception("emitting a body-too-large refusal failed")
        finally:
            # The body was refused unread, so the connection goes regardless.
            transport.close()
