"""The synchronous WebSocket session the test client opens."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import veloce._json as _json
from veloce._protocol_constants import (
    ASGI_EVENT_WS_ACCEPT,
    ASGI_EVENT_WS_CLOSE,
    ASGI_EVENT_WS_CONNECT,
    ASGI_EVENT_WS_DISCONNECT,
    ASGI_EVENT_WS_RECEIVE,
    ASGI_SCOPE_WEBSOCKET,
    URL_SCHEME_WS,
)
from veloce.status import (
    WS_1000_NORMAL_CLOSURE,
)

if TYPE_CHECKING:  # pragma: no cover
    from veloce.testclient.client import TestClient

    pass
from veloce.testclient._headers import _header_pairs, _HeadersArg


class _WebSocketSession:
    """Sync wrapper around an in-memory ASGI websocket session.

    The handler runs as a background task on the TestClient's loop, and
    `send_text` / `receive_text` / `close` route through two asyncio queues that
    play the part the network would.
    """

    def __init__(
        self,
        client: TestClient,
        path: str,
        subprotocols: list[str] | None,
        headers: _HeadersArg,
    ) -> None:
        self._client = client
        self._path = path
        self._subprotocols = subprotocols or []
        self._headers = headers or {}
        self._to_handler: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_handler: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._handler_task: asyncio.Task[None] | None = None
        self.accepted_subprotocol: str | None = None

    def __enter__(self) -> _WebSocketSession:
        async def _start() -> None:
            scope_headers: list[tuple[bytes, bytes]] = []
            if self._subprotocols:
                scope_headers.append(
                    (
                        b"sec-websocket-protocol",
                        ", ".join(self._subprotocols).encode("latin-1"),
                    )
                )
            seen_host = False
            for k, v in _header_pairs(self._headers):
                if k.lower() == "host":
                    seen_host = True
                scope_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
            # RFC 6455 Sec. 4.1 requires `Host` on the opening handshake; without it an
            # app running host validation refuses every in-memory socket.
            if not seen_host:
                scope_headers.append((b"host", b"testserver"))

            # Split as `_make_request` does, or route matching sees the `?` and
            # `WebSocket.query_params` stays empty.
            ws_path, _, ws_query = self._path.partition("?")

            scope = {
                "type": ASGI_SCOPE_WEBSOCKET,
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "scheme": URL_SCHEME_WS,
                "path": unquote(ws_path),
                "raw_path": ws_path.encode("utf-8"),
                "query_string": ws_query.encode("ascii"),
                "root_path": "",
                "headers": scope_headers,
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
                "subprotocols": list(self._subprotocols),
            }

            async def receive() -> dict[str, Any]:
                return await self._to_handler.get()

            async def send(msg: dict[str, Any]) -> None:
                await self._from_handler.put(msg)

            # Kick the handshake.
            await self._to_handler.put({"type": ASGI_EVENT_WS_CONNECT})
            self._handler_task = asyncio.create_task(self._client.app(scope, receive, send))

            # Wait for the accept (or close) frame before returning.
            first = await self._from_handler.get()
            if first["type"] == ASGI_EVENT_WS_CLOSE:
                code = first.get("code", WS_1000_NORMAL_CLOSURE)
                raise RuntimeError(f"WebSocket rejected with close code {code}")
            if first["type"] != ASGI_EVENT_WS_ACCEPT:
                raise RuntimeError(
                    f"WebSocket handshake produced an unexpected ASGI message: {first['type']!r}"
                )
            self.accepted_subprotocol = first.get("subprotocol")

        self._client._loop.run_until_complete(_start())
        return self

    def __exit__(self, *exc: Any) -> None:
        async def _close() -> None:
            if self._handler_task and not self._handler_task.done():
                await self._to_handler.put(
                    {"type": ASGI_EVENT_WS_DISCONNECT, "code": WS_1000_NORMAL_CLOSURE}
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._handler_task, timeout=1.0)

        self._client._loop.run_until_complete(_close())

    def send_text(self, data: str) -> None:
        """Send a text frame to the websocket handler."""
        self._client._loop.run_until_complete(
            self._to_handler.put({"type": ASGI_EVENT_WS_RECEIVE, "text": data})
        )

    def send_bytes(self, data: bytes) -> None:
        """Send a binary frame to the websocket handler."""
        self._client._loop.run_until_complete(
            self._to_handler.put({"type": ASGI_EVENT_WS_RECEIVE, "bytes": data})
        )

    def _receive(self, key: str) -> Any:
        """Pull the next frame and return its `key` payload (`text`/`bytes`)."""

        async def _r() -> Any:
            msg = await self._from_handler.get()
            if msg.get("type") == ASGI_EVENT_WS_CLOSE:
                raise RuntimeError(f"WebSocket closed: {msg.get('code', WS_1000_NORMAL_CLOSURE)}")
            return msg[key]

        return self._client._loop.run_until_complete(_r())

    def receive_text(self) -> str:
        """Receive the next text frame sent by the handler."""
        return self._receive("text")

    def receive_bytes(self) -> bytes:
        """Receive the next binary frame sent by the handler."""
        return self._receive("bytes")

    def receive_json(self) -> Any:
        """Receive the next text frame and parse it as JSON."""
        return _json.loads(self.receive_text())

    def send_json(self, data: Any) -> None:
        """Send `data` to the handler as a JSON-encoded text frame."""
        self.send_text(_json.dumps(data).decode("utf-8"))
