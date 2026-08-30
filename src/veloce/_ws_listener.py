"""Declarative WebSocket listener — the accept/receive/dispatch/close loop.

`Router.websocket_listener` wraps a per-message callback into a full WebSocket
handler: accept, receive-loop, dispatch, clean disconnect. The loop builder
lives here rather than in `routing/` so the router stays free of WebSocket
internals, and rather than in `websocket.py` so the one module a maintainer
opens to change frame parsing is not also the one they open to change
listener-callback binding. Nothing here reaches past the public `WebSocket`
API.

`WebSocket` is imported at runtime, not under `TYPE_CHECKING`: the dependency
resolver reads the built handler's annotations through `get_type_hints`, which
evaluates them against this module's globals.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from veloce._internal import _is_async_callable, offload
from veloce.exceptions import WebSocketDisconnect
from veloce.websocket import WebSocket

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Awaitable, Callable, Coroutine


_WS_MODES = frozenset({"text", "bytes", "json"})


def _resolve_listener_callable(
    callback: Any,
) -> tuple[Callable[..., Awaitable[Any]], bool]:
    """Return an async-callable form of `callback` and whether it wants the socket.

    A sync callback is offloaded to the default executor so a blocking
    per-message body never stalls the event loop, matching how the framework
    runs sync HTTP handlers. The socket is passed positionally as the first
    argument when the callback declares a leading `ws`/`socket` parameter or
    accepts two or more positional parameters.
    """
    wants_socket = _callback_wants_socket(callback)
    if _is_async_callable(callback):
        return callback, wants_socket

    async def _async_call(*args: Any) -> Any:
        # `offload` preserves the request-scoped ContextVars a sync HTTP
        # handler sees (`current_app` / `g` / `request`).
        return await offload(callback, *args)

    return _async_call, wants_socket


def _callback_wants_socket(callback: Any) -> bool:
    """Decide whether a listener callback expects the socket as its first arg.

    True when the first positional parameter is named `ws` or `socket`, or
    when the callback accepts two or more positional parameters (so the data
    is the second). A single-parameter `on_receive(data)` callback gets only
    the message.
    """
    # `inspect.signature` already unwraps a callable instance's `__call__`
    # and drops the bound `self`, so it works on plain functions, bound
    # methods, and `__call__`-able objects alike.
    try:
        params = [
            p
            for p in inspect.signature(callback).parameters.values()
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        return False
    if not params:
        return False
    if params[0].name in ("ws", "socket"):
        return True
    return len(params) >= 2


async def _listener_receive(ws: WebSocket, mode: str) -> Any:
    if mode == "text":
        return await ws.receive_text()
    if mode == "bytes":
        return await ws.receive_bytes()
    return await ws.receive_json()


async def _listener_send(ws: WebSocket, mode: str, data: Any) -> None:
    if mode == "text":
        await ws.send_text(data if isinstance(data, str) else str(data))
    elif mode == "bytes":
        await ws.send_bytes(data)
    else:
        await ws.send_json(data)


def build_listener_handler(
    callback: Any,
    *,
    receive: str = "json",
    send: str = "json",
    on_connect: Any = None,
    on_disconnect: Any = None,
) -> Callable[[WebSocket], Coroutine[Any, Any, None]]:
    """Build a WebSocket handler that runs the canonical accept/receive/close loop.

    The returned handler accepts the connection, fires `on_connect`, then
    loops: receive one message in `receive` mode, pass it to `callback`, and
    send the return value in `send` mode when it is not `None`. The loop ends
    on `WebSocketDisconnect`; `on_disconnect` always runs afterwards. A
    callback that returns `None` sends nothing, so a pure consumer needs no
    special casing.
    """
    if receive not in _WS_MODES:
        raise ValueError(f"receive mode must be one of {sorted(_WS_MODES)}, got {receive!r}")
    if send not in _WS_MODES:
        raise ValueError(f"send mode must be one of {sorted(_WS_MODES)}, got {send!r}")

    fn, wants_socket = _resolve_listener_callable(callback)
    connect_fn = _resolve_listener_callable(on_connect)[0] if on_connect is not None else None
    disconnect_fn = (
        _resolve_listener_callable(on_disconnect)[0] if on_disconnect is not None else None
    )

    # Deliberately NOT `functools.wraps(callback)`: the registered handler
    # must present its own `(ws: WebSocket)` signature so the dependency
    # resolver injects the socket. `wraps` sets `__wrapped__`, which
    # `inspect.signature` follows back to the callback's `(data)` shape and
    # makes the resolver try to bind a nonexistent `data` dependency.
    async def listener(ws: WebSocket) -> None:
        await ws.accept()
        try:
            if connect_fn is not None:
                await connect_fn(ws)
            while True:
                data = await _listener_receive(ws, receive)
                result = await (fn(ws, data) if wants_socket else fn(data))
                # A `None` return means "consume only" - never emit a frame
                # for it (sending `null`/empty would be a spurious message).
                if result is not None:
                    await _listener_send(ws, send, result)
        except WebSocketDisconnect:
            # Peer (or idle/heartbeat close) ended the connection - the
            # canonical, non-error way a listener loop terminates.
            pass
        finally:
            if disconnect_fn is not None:
                # Run teardown even if the peer is already gone; a send from
                # inside `on_disconnect` may itself raise, which is fine.
                await disconnect_fn(ws)

    # Borrow the callback's name for routing/OpenAPI introspection without
    # importing its signature (see the no-`wraps` note above).
    listener.__name__ = getattr(callback, "__name__", "listener")
    return listener
