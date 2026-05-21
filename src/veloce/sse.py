"""Server-Sent Events (SSE) — streaming event responses."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from veloce.http.response import Response, _reject_header_crlf


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
        lines = []
        if self.id is not None:
            lines.append(f"id: {self.id}")
        if self.event is not None:
            lines.append(f"event: {self.event}")
        if self.retry is not None:
            lines.append(f"retry: {self.retry}")
        for line in self.data.split("\n"):
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

    def __init__(
        self,
        content: AsyncIterator[ServerSentEvent | str | bytes],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        hdrs = headers or {}
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
        from http import HTTPStatus

        reason = HTTPStatus(self.status_code).phrase
        parts = [f"HTTP/1.1 {self.status_code} {reason}\r\n"]
        for key, value in {
            "Content-Type": self.content_type,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            **self.headers,
        }.items():
            _reject_header_crlf(str(value), f"{key} header value")
            parts.append(f"{key}: {value}\r\n")
        parts.append("\r\n")
        transport.write("".join(parts).encode("latin-1"))

        async for chunk in self._stream:
            # `_stream` is normalised to bytes by `_encode_stream`.
            size = format(len(chunk), "x")
            transport.write(f"{size}\r\n".encode() + chunk + b"\r\n")

        transport.write(b"0\r\n\r\n")
