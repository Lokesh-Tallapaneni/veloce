"""RSGI transport — the RSGI server entry point, translated to the ASGI shape.

RSGI hands each connection its own ``(scope, protocol)`` pair rather than
ASGI's ``(scope, receive, send)`` triple, and its HTTP
response calls are synchronous rather than message-based. Translating one
shape into the other here, once, lets ``__rsgi__`` delegate to ``_asgi_app``
entirely - the compiled ASGI middleware wrap, mounts, ``MAX_CONTENT_LENGTH``,
and ``stream=True`` routes all already work there, and must be reached
rather than re-implemented a second time.
"""

from __future__ import annotations

import inspect
from typing import Any

from veloce._protocol_constants import (
    ASGI_EVENT_HTTP_DISCONNECT,
    ASGI_EVENT_HTTP_REQUEST,
    ASGI_EVENT_HTTP_RESPONSE_START,
    ASGI_EVENT_WS_ACCEPT,
    ASGI_EVENT_WS_CLOSE,
    ASGI_EVENT_WS_CONNECT,
    ASGI_EVENT_WS_SEND,
    ASGI_SCOPE_HTTP,
    ASGI_SCOPE_WEBSOCKET,
    LIFECYCLE_SHUTDOWN,
    LIFECYCLE_STARTUP,
)
from veloce.app._host import AppHost


async def _maybe_await(value: Any) -> Any:
    """Resolve a call that may or may not be a coroutine.

    Every RSGI HTTP response method is a plain function today, not a
    coroutine; nothing in the protocol promises that stays true, so calls
    through here keep working either way.
    """
    if inspect.isawaitable(value):
        return await value
    return value


def _rsgi_headers_to_asgi(headers: Any) -> list[tuple[bytes, bytes]]:
    """Convert RSGI's decoded-string header view to ASGI's raw-bytes list.

    HTTP header bytes are normatively ASCII/latin-1 (RFC 7230 Sec. 3.2.4), so
    a conformant request round-trips cleanly through whatever decoding RSGI
    already applied; `errors="replace"` only matters for a non-conformant
    header RSGI decoded some other way, where losing that one byte is better
    than failing the whole connection on it.
    """
    return [
        (k.encode("latin-1", errors="replace"), v.encode("latin-1", errors="replace"))
        for k, v in headers.items()
    ]


def _split_host_port(value: str) -> tuple[str, int]:
    """Split an RSGI `host:port` address, handling IPv6 brackets.

    A bracketed address (`[::1]:8080`) has its brackets stripped to match
    ASGI's own convention; a bare IPv6 host (two or more colons, no bracket)
    is kept whole rather than guessing which colon is the port separator -
    RSGI's own spec promises a port is always present, but says nothing
    about a bracket convention for the unbracketed case.
    """
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port_str = rest[1:] if rest.startswith(":") else ""
    elif value.count(":") >= 2:
        return value, 0
    else:
        host, _, port_str = value.rpartition(":")
    try:
        return host, int(port_str)
    except ValueError:
        return host or value, 0


class _RsgiHttpReceive:
    """Adapts an RSGI HTTP protocol's body stream to ASGI's `receive()` shape."""

    __slots__ = ("_body", "_done")

    def __init__(self, protocol: Any) -> None:
        self._body = protocol.__aiter__()
        self._done = False

    async def __call__(self) -> dict[str, Any]:
        if self._done:
            return {"type": ASGI_EVENT_HTTP_DISCONNECT}
        try:
            chunk = await self._body.__anext__()
        except StopAsyncIteration:
            self._done = True
            return {"type": ASGI_EVENT_HTTP_REQUEST, "body": b"", "more_body": False}
        return {"type": ASGI_EVENT_HTTP_REQUEST, "body": chunk, "more_body": True}


class _RsgiHttpSend:
    """Adapts ASGI's two-phase `send()` to RSGI's response calls.

    `_asgi_app` always sends one `http.response.start`, then either one
    `http.response.body` with no `more_body` (buffered) or a `more_body=True`
    run followed by a final empty one (`_emit_stream`'s own shape). The first
    body message decides which RSGI call this becomes; once a stream has
    started, every later chunk goes straight to its transport.
    """

    __slots__ = ("_protocol", "_status", "_headers", "_stream")

    def __init__(self, protocol: Any) -> None:
        self._protocol = protocol
        self._status = 200
        self._headers: list[tuple[str, str]] = []
        self._stream: Any = None

    async def __call__(self, message: dict[str, Any]) -> None:
        if message["type"] == ASGI_EVENT_HTTP_RESPONSE_START:
            self._status = message["status"]
            self._headers = [
                (k.decode("latin-1"), v.decode("latin-1")) for k, v in message["headers"]
            ]
            return
        body = message.get("body", b"") or b""
        if self._stream is not None:
            if body:
                await self._stream.send_bytes(body)
            return
        if not message.get("more_body", False):
            if body:
                await _maybe_await(self._protocol.response_bytes(self._status, self._headers, body))
            else:
                await _maybe_await(self._protocol.response_empty(self._status, self._headers))
            return
        self._stream = await _maybe_await(
            self._protocol.response_stream(self._status, self._headers)
        )
        if body:
            await self._stream.send_bytes(body)


class _RsgiWsBridge:
    """Adapts an RSGI WebSocket `(scope, protocol)` pair to ASGI's message pair.

    RSGI has no handshake message to consume - the connection already exists
    when `__rsgi__` is called - so the bridge's first `receive()` manufactures
    `websocket.connect` itself, matching what `WebSocket.from_asgi`'s
    `accept()` expects to see before it sends `websocket.accept`. RSGI's
    `close()` takes no reason, so one passed to `send()` is dropped.
    """

    __slots__ = ("_protocol", "_transport", "_sent_connect")

    def __init__(self, protocol: Any) -> None:
        self._protocol = protocol
        self._transport: Any = None
        self._sent_connect = False

    async def receive(self) -> dict[str, Any]:
        if not self._sent_connect:
            self._sent_connect = True
            return {"type": ASGI_EVENT_WS_CONNECT}
        message = await self._transport.receive()
        if message.kind == 0:  # closed by the peer; RSGI gives no code
            return {"type": "websocket.disconnect"}
        if message.kind == 2:
            return {"type": "websocket.receive", "text": message.data}
        return {"type": "websocket.receive", "bytes": message.data}

    async def send(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == ASGI_EVENT_WS_ACCEPT:
            self._transport = await self._protocol.accept()
            return
        if kind == ASGI_EVENT_WS_SEND:
            if "text" in message:
                await self._transport.send_str(message["text"])
            else:
                await self._transport.send_bytes(message["bytes"])
            return
        if kind == ASGI_EVENT_WS_CLOSE:
            self._protocol.close(message.get("code"))
            return


class RsgiMixin(AppHost):
    """The RSGI server entry point, mixed into `Veloce`."""

    def __rsgi_init__(self, loop: Any) -> None:
        """Run startup hooks before the server starts serving.

        RSGI calls this with the event loop not yet running, so the
        coroutine is driven to completion here rather than awaited.
        """
        loop.run_until_complete(self._run_lifecycle(LIFECYCLE_STARTUP))

    def __rsgi_del__(self, loop: Any) -> None:
        """Run shutdown hooks as the server tears down, same loop contract."""
        loop.run_until_complete(self._run_lifecycle(LIFECYCLE_SHUTDOWN))

    async def __rsgi__(self, scope: Any, protocol: Any) -> None:
        """Serve one RSGI connection - one HTTP request, or one WebSocket.

        Routed through `__call__`, not `_asgi_app`/`_asgi_websocket` directly:
        `__call__` is what applies the compiled ASGI middleware wrap when one
        is registered, and `_asgi_app`'s own `scope["type"]` branch is what
        matches an ASGI mount - for both HTTP and WebSocket - before ever
        reaching `_asgi_websocket`. Calling either helper directly skips both.
        """
        if scope.proto == "http":
            await self.__call__(
                self._rsgi_http_scope(scope), _RsgiHttpReceive(protocol), _RsgiHttpSend(protocol)
            )
            return
        bridge = _RsgiWsBridge(protocol)
        await self.__call__(self._rsgi_ws_scope(scope), bridge.receive, bridge.send)

    def _rsgi_scope_common(self, scope: Any, scope_type: str) -> dict[str, Any]:
        server_host, server_port = _split_host_port(scope.server)
        client_host, client_port = _split_host_port(scope.client)
        return {
            "type": scope_type,
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": scope.http_version,
            "path": scope.path,
            "raw_path": scope.path.encode("utf-8"),
            "root_path": "",
            "query_string": scope.query_string.encode("latin-1"),
            "headers": _rsgi_headers_to_asgi(scope.headers),
            "server": (server_host, server_port),
            "client": (client_host, client_port),
            "app": self,
        }

    def _rsgi_http_scope(self, scope: Any) -> dict[str, Any]:
        asgi_scope = self._rsgi_scope_common(scope, ASGI_SCOPE_HTTP)
        asgi_scope["method"] = scope.method
        asgi_scope["scheme"] = scope.scheme
        return asgi_scope

    def _rsgi_ws_scope(self, scope: Any) -> dict[str, Any]:
        asgi_scope = self._rsgi_scope_common(scope, ASGI_SCOPE_WEBSOCKET)
        asgi_scope["scheme"] = "wss" if scope.scheme == "https" else "ws"
        return asgi_scope
