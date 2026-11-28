"""Receiving — state checks, send drain, idle timeout and the `receive_*` / `iter_*` readers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, NoReturn, cast

import veloce._json as _json
from veloce._protocol_constants import (
    ASGI_EVENT_WS_DISCONNECT,
)
from veloce.exceptions import WebSocketDisconnect
from veloce.status import (
    WS_1000_NORMAL_CLOSURE,
    WS_1001_GOING_AWAY,
)
from veloce.websocket.frames import (
    _RAW_DISCONNECT,
)
from veloce.websocket.handshake import (
    _validate_idle_timeout,
)

if TYPE_CHECKING:  # pragma: no cover
    pass


_logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover

    class _Host:
        """The `WebSocket` members this mixin reads, declared for the type checker only."""

        transport: Any
        close_code: int | None
        _accepted: bool
        _closed: bool
        _idle_timeout: float | None
        _receive_queue: asyncio.Queue[bytes | object] | None
        _send_drain: Any
        _send_gate: Any
        _pending_pong: bytes | None
        _asgi_receive: Any
        _asgi_send: Any

        @property
        def _is_asgi(self) -> bool: ...
        async def close(self, code: int = ..., reason: str = ...) -> None: ...
        async def _asgi_recv_msg(self) -> dict[str, Any]: ...
        def _send_pong(self, payload: bytes) -> None: ...
        def _send_frame(self, data: bytes, opcode: int) -> None: ...
        def _asgi_disconnected(self, msg: dict[str, Any]) -> NoReturn: ...

else:
    _Host = object


class ReceiveMixin(_Host):
    """Receive, drain and idle-timeout handling for a `WebSocket`."""

    __slots__ = ()

    def _check_can_receive(self, method: str) -> None:
        """Enforce the handshake state machine for receive operations.

        Without them, a receive before `accept()` hangs on an empty queue or on the
        ASGI callable with no clear failure mode. A receive on an already-closed
        connection is a `WebSocketDisconnect`.
        """
        if not self._accepted:
            raise RuntimeError(f"WebSocket.{method}(): call accept() before receiving")
        if self._closed:
            # The recorded code when a close arrived between receives, not 1000.
            raise WebSocketDisconnect(self.close_code or WS_1000_NORMAL_CLOSURE)

    def set_send_gate(self, ready: Any) -> None:
        """Install the sync write-readiness predicate (raw transport only).

        The automatic PONG reply runs inside the synchronous frame parser, which
        cannot await `set_send_drain`'s hook, so `ready()` reports whether the
        transport will accept a write now and the PONG is deferred when it will not.
        """
        self._send_gate = ready

    def flush_pending_pong(self) -> None:
        """Send the deferred PONG, if any, once writing is possible again."""
        payload, self._pending_pong = self._pending_pong, None
        if payload is not None:
            self._send_frame(payload, opcode=0xA)

    def set_send_drain(self, drain: Any) -> None:
        """Install the native write-side backpressure hook (raw transport only).

        `drain` is an awaitable-returning callable that blocks while the transport's
        outgoing buffer is over its high-water mark, so a slow-reading client
        suspends the producing handler instead of letting that buffer grow.
        """
        self._send_drain = drain

    def set_idle_timeout(self, idle_timeout: float | None) -> None:
        """Set the idle-receive timeout in seconds (`None` disables it).

        Applies to every subsequent blocking receive on this connection.
        Call it inside the handler (typically right after `accept()`) to
        enable or adjust the window; passing `idle_timeout=` to
        `WebSocket.from_asgi` sets the same value at construction.
        """
        _validate_idle_timeout(idle_timeout)
        self._idle_timeout = idle_timeout

    def _effective_timeout(self, timeout: float | None) -> float | None:
        """Bound a per-call timeout by the connection's idle timeout.

        The smaller of the per-call `timeout` and the configured `idle_timeout`;
        either being `None` falls back to the other.
        """
        idle = self._idle_timeout
        if idle is None:
            return timeout
        if timeout is None:
            return idle
        return min(timeout, idle)

    async def _idle_close(self) -> NoReturn:
        """Close cleanly on an idle timeout and signal the handler.

        Closes with `1001 Going Away`, never a fabricated 1006, then raises
        `WebSocketDisconnect` so a receive or `iter_*` loop unwinds as on a peer
        close.
        """
        with contextlib.suppress(Exception):
            await self.close(code=WS_1001_GOING_AWAY)
        raise WebSocketDisconnect(WS_1001_GOING_AWAY)

    async def receive_text(self, timeout: float | None = None) -> str:
        """Receive a text message. Raises asyncio.TimeoutError if timeout exceeded.

        When `idle_timeout` is configured, a wait longer than the idle
        window closes the connection with `1001 Going Away` and raises
        `WebSocketDisconnect` instead of `asyncio.TimeoutError`.
        """
        self._check_can_receive("receive_text")
        if self._asgi_send is not None:
            # Taken here, not through the wrappers: with no deadline, nothing to arm.
            if timeout is None and self._idle_timeout is None:
                msg = await self._asgi_receive()
                if msg["type"] == ASGI_EVENT_WS_DISCONNECT:
                    self._asgi_disconnected(msg)
            else:
                msg = await self._asgi_recv_text_or_bytes(timeout)
            return msg.get("text") or (msg.get("bytes") or b"").decode("utf-8")
        data = await self._raw_recv(timeout)
        return data.decode("utf-8") if isinstance(data, bytes) else str(data)

    async def _raw_recv(self, timeout: float | None) -> bytes:
        """Receive one complete message in raw mode, bounded by the idle window.

        In raw-transport mode the idle window is measured per completed message, so a
        peer that only streams long fragmented messages or control frames can trip
        it. An idle expiry performs the `1001 Going Away` close; a binding per-call
        `timeout` raises `asyncio.TimeoutError` unchanged.
        """
        eff = self._effective_timeout(timeout)
        queue = self._receive_queue
        assert queue is not None  # raw transport only; the ASGI path never reaches here
        if eff is None:
            item = await queue.get()
        else:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=eff)
            except (TimeoutError, asyncio.TimeoutError):
                await self._maybe_idle_timeout(timeout, eff)
                raise
        # The parser's terminal sentinel; a close frame may omit the code.
        if item is _RAW_DISCONNECT:
            raise WebSocketDisconnect(self.close_code or WS_1000_NORMAL_CLOSURE)
        return cast("bytes", item)

    async def _maybe_idle_timeout(self, timeout: float | None, eff: float | None) -> None:
        """Treat a `wait_for` timeout as an idle close when idle won the race.

        A timeout means "idle" only when the effective deadline came from the idle
        window; when the per-call `timeout` was the binding one, the original
        `asyncio.TimeoutError` propagates unchanged.
        """
        if self._idle_timeout is not None and (timeout is None or eff == self._idle_timeout):
            await self._idle_close()

    async def _asgi_recv_text_or_bytes(self, timeout: float | None) -> dict[str, Any]:
        """Receive one ASGI message, bounding the wait only when a deadline is set.

        The common case has neither deadline, so the receive awaits directly and
        skips the `asyncio.wait_for` Task wrapper.
        """
        eff = self._effective_timeout(timeout)
        if eff is None:
            return await self._asgi_recv_msg()
        try:
            return await asyncio.wait_for(self._asgi_recv_msg(), timeout=eff)
        except (TimeoutError, asyncio.TimeoutError):
            await self._maybe_idle_timeout(timeout, eff)
            raise

    async def receive_json(self, timeout: float | None = None) -> Any:
        """Receive and parse JSON."""
        text = await self.receive_text(timeout=timeout)
        return _json.loads(text)

    async def receive_bytes(self, timeout: float | None = None) -> bytes:
        """Receive binary data. Raises asyncio.TimeoutError if timeout exceeded.

        When `idle_timeout` is configured, a wait longer than the idle
        window closes the connection with `1001 Going Away` and raises
        `WebSocketDisconnect` instead of `asyncio.TimeoutError`.
        """
        self._check_can_receive("receive_bytes")
        if self._asgi_send is not None:
            # Taken here, not through the wrappers: with no deadline, nothing to arm.
            if timeout is None and self._idle_timeout is None:
                msg = await self._asgi_receive()
                if msg["type"] == ASGI_EVENT_WS_DISCONNECT:
                    self._asgi_disconnected(msg)
            else:
                msg = await self._asgi_recv_text_or_bytes(timeout)
            return msg.get("bytes") or msg.get("text", "").encode("utf-8")
        return await self._raw_recv(timeout)

    async def iter_text(self) -> Any:
        """Async-iterate over incoming text frames until the peer closes.

        Usage::

            async for msg in ws.iter_text():
                ...

        Terminates cleanly on `WebSocketDisconnect`. Other exceptions
        propagate.
        """
        try:
            while True:
                yield await self.receive_text()
        except WebSocketDisconnect:
            return

    async def iter_bytes(self) -> Any:
        """Async-iterate over incoming binary frames until the peer closes."""
        try:
            while True:
                yield await self.receive_bytes()
        except WebSocketDisconnect:
            return

    async def iter_json(self) -> Any:
        """Async-iterate over incoming JSON-decoded frames until peer closes."""
        try:
            while True:
                yield await self.receive_json()
        except WebSocketDisconnect:
            return
