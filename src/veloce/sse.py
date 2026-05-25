"""Server-Sent Events (SSE) — streaming event responses."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from veloce._internal import _STATUS_PHRASES, _reject_header_crlf
from veloce.http.response import Response


class ServerSentEvent:
    """A single SSE event."""

    __slots__ = ("data", "event", "id", "retry")

    def __init__(
        self,
        data: str,
        event: str | None = None,
        id: str | None = None,
        retry: int | None = None,
    ) -> None:
        self.data = data
        self.event = event
        self.id = id
        self.retry = retry

    def encode(self) -> bytes:
        """Encode the event as an SSE-formatted byte string."""
        lines = []
        if self.id is not None:
            clean_id = str(self.id).replace("\n", "").replace("\r", "")
            lines.append(f"id: {clean_id}")
        if self.event is not None:
            clean_event = self.event.replace("\n", "").replace("\r", "")
            lines.append(f"event: {clean_event}")
        if self.retry is not None:
            lines.append(f"retry: {self.retry}")
        data = self.data.replace("\r\n", "\n").replace("\r", "\n")
        # Single-line payloads — by far the common case — skip the
        # `split("\n")` allocation and emit the field directly.
        if "\n" not in data:
            lines.append(f"data: {data}")
        else:
            for line in data.split("\n"):
                lines.append(f"data: {line}")
        lines.append("")
        lines.append("")
        return "\n".join(lines).encode("utf-8")


class EventSourceResponse(Response):
    """SSE streaming response — sends events over a long-lived connection.

    Usage:
        @app.get("/events")
        async def events(request: Request):
            async def generate():
                for i in range(10):
                    yield ServerSentEvent(data=f"Event {i}")
                    await asyncio.sleep(1)
            return EventSourceResponse(generate())
    """

    is_event_source = True

    def __init__(
        self,
        content: AsyncIterator[ServerSentEvent | str | bytes],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        hdrs = dict(headers) if headers else {}
        hdrs.update(
            {
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
        super().__init__(
            status_code=status_code,
            body=b"",
            content_type="text/event-stream",
            headers=hdrs,
        )
        # Normalise every yielded item to bytes up front, so the ASGI
        # transport and the raw-socket transport consume an identical
        # `bytes` stream — see `_encode_stream`.
        self._stream = self._encode_stream(content)

    @staticmethod
    async def _encode_stream(
        content: AsyncIterator[ServerSentEvent | str | bytes],
    ) -> AsyncIterator[bytes]:
        """Encode each yielded item to `bytes`.

        Accepts `ServerSentEvent` objects (encoded via `.encode()`), plain
        `str` (UTF-8 encoded), or already-encoded `bytes`. This keeps both
        transports consistent: the ASGI streaming branch and `stream_to`
        each receive `bytes` regardless of what the handler yields.
        """
        async for item in content:
            if isinstance(item, ServerSentEvent):
                yield item.encode()
            elif isinstance(item, str):
                yield item.encode("utf-8")
            else:
                yield item

    async def stream_to(self, transport: Any) -> None:
        """Stream SSE events to transport."""
        reason = _STATUS_PHRASES.get(self.status_code, "")
        parts = [f"HTTP/1.1 {self.status_code} {reason}".rstrip() + "\r\n"]
        final_headers = {
            "Content-Type": self.content_type,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            **self.headers,
        }
        for key, value in final_headers.items():
            k_lower = key.lower()
            if k_lower == "set-cookie":
                for cookie_line in str(value).split("\r\nSet-Cookie: "):
                    _reject_header_crlf(cookie_line, "Set-Cookie value")
                    parts.append(f"Set-Cookie: {cookie_line}\r\n")
            else:
                _reject_header_crlf(str(key), "header name")
                _reject_header_crlf(str(value), f"{key} header value")
                parts.append(f"{key}: {value}\r\n")
        parts.append("\r\n")
        transport.write("".join(parts).encode("latin-1"))

        async for chunk in self._stream:
            # `_stream` is normalised to bytes by `_encode_stream`.
            # `writelines` keeps the size-line, payload, and trailer as
            # separate buffers instead of concatenating them into a fresh
            # bytes object per chunk.
            size = format(len(chunk), "x").encode("ascii")
            transport.writelines((size, b"\r\n", chunk, b"\r\n"))

        transport.write(b"0\r\n\r\n")
