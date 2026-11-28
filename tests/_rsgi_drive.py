"""A fake RSGI scope/protocol pair, driving `RsgiMixin.__rsgi__` in tests.

RSGI's real scope and protocol objects are implemented in Rust and have no
public Python constructor, so parity tests drive this stand-in instead -
built to the shapes documented by the RSGI specification (scope fields,
synchronous `response_*` calls, the HTTP body async iterator, and the
WebSocket transport's `receive`/`send_bytes`/`send_str`/`close`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


class FakeRsgiHeaders:
    """A minimal stand-in for the RSGI header view: `.items()` is all `_rsgi_headers_to_asgi` needs."""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def items(self) -> list[tuple[str, str]]:
        return list(self._pairs)

    def get(self, key: str, default: Any = None) -> Any:
        key = key.lower()
        for k, v in self._pairs:
            if k.lower() == key:
                return v
        return default


@dataclass
class FakeRsgiScope:
    proto: str
    method: str = "GET"
    path: str = "/"
    query_string: str = ""
    scheme: str = "http"
    http_version: str = "1.1"
    rsgi_version: str = "1.6"
    server: str = "127.0.0.1:8000"
    client: str = "127.0.0.1:51234"
    authority: str | None = None
    headers: FakeRsgiHeaders = field(default_factory=lambda: FakeRsgiHeaders([]))


class FakeRsgiHttpStreamTransport:
    """The transport `response_stream` hands back."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        self.chunks.append(data)

    async def send_str(self, data: str) -> None:
        self.chunks.append(data.encode("utf-8"))


class FakeRsgiHttpProtocol:
    """Drives one HTTP request: a body iterator in, a recorded response out."""

    def __init__(self, body: bytes = b"", *, chunk_size: int | None = None) -> None:
        self._body = body
        self._chunk_size = chunk_size
        self.status: int | None = None
        self.headers: list[tuple[str, str]] | None = None
        self.body: bytes | None = None
        self.stream: FakeRsgiHttpStreamTransport | None = None
        self.file: tuple[str, int | None, int | None] | None = None

    async def __call__(self) -> bytes:
        return self._body

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter_body()

    async def _iter_body(self) -> AsyncIterator[bytes]:
        size = self._chunk_size or len(self._body) or 1
        for i in range(0, len(self._body), size):
            yield self._body[i : i + size]

    async def client_disconnect(self) -> None:
        await asyncio.Event().wait()  # pragma: no cover - never awaited in these tests

    def response_empty(self, status: int, headers: list[tuple[str, str]]) -> None:
        self.status, self.headers, self.body = status, headers, b""

    def response_str(self, status: int, headers: list[tuple[str, str]], body: str) -> None:
        self.status, self.headers, self.body = status, headers, body.encode("utf-8")

    def response_bytes(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status, self.headers, self.body = status, headers, body

    def response_file(self, status: int, headers: list[tuple[str, str]], file: str) -> None:
        self.status, self.headers, self.file = status, headers, (file, None, None)

    def response_file_range(
        self, status: int, headers: list[tuple[str, str]], file: str, start: int, end: int
    ) -> None:
        self.status, self.headers, self.file = status, headers, (file, start, end)

    def response_stream(
        self, status: int, headers: list[tuple[str, str]]
    ) -> FakeRsgiHttpStreamTransport:
        self.status, self.headers = status, headers
        self.stream = FakeRsgiHttpStreamTransport()
        return self.stream

    @property
    def streamed_body(self) -> bytes:
        """Join whatever `response_stream`'s transport collected."""
        return b"".join(self.stream.chunks) if self.stream is not None else b""


class FakeRsgiWebsocketTransport:
    """The transport `accept()` hands back: queued inbound, recorded outbound."""

    def __init__(self, inbound: list[Any]) -> None:
        self._inbound = list(inbound)
        self.sent: list[tuple[str, bytes | str]] = []

    async def receive(self) -> Any:
        if not self._inbound:
            return _FakeWebsocketMessage(0, None)
        return self._inbound.pop(0)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(("bytes", data))

    async def send_str(self, data: str) -> None:
        self.sent.append(("str", data))


@dataclass
class _FakeWebsocketMessage:
    kind: int
    data: bytes | str | None


def ws_text(data: str) -> _FakeWebsocketMessage:
    return _FakeWebsocketMessage(2, data)


def ws_bytes(data: bytes) -> _FakeWebsocketMessage:
    return _FakeWebsocketMessage(1, data)


def ws_close() -> _FakeWebsocketMessage:
    return _FakeWebsocketMessage(0, None)


class FakeRsgiWebsocketProtocol:
    """Drives one WebSocket connection: accept, exchange messages, close."""

    def __init__(self, inbound: list[Any] | None = None) -> None:
        self._inbound = inbound or []
        self.transport: FakeRsgiWebsocketTransport | None = None
        self.close_status: int | None | object = _NOT_CLOSED

    async def accept(self) -> FakeRsgiWebsocketTransport:
        self.transport = FakeRsgiWebsocketTransport(self._inbound)
        return self.transport

    def close(self, status: int | None) -> tuple[int, bool]:
        self.close_status = status
        return (status or 1000, True)


_NOT_CLOSED = object()
