"""The bounded and unbounded ASGI message paths behave identically.

A receive with no per-call `timeout` and no configured `idle_timeout` has no
deadline to arm, so it reads the ASGI message itself rather than going through
the timeout and envelope wrappers — that is the frame every message on every
connection pays. The same reasoning inlines the send.

The risk this buys is divergence: two ways in, one contract. The peer-close and
dead-peer handling therefore stay in `_asgi_disconnected` / `_asgi_send_failed`,
and these tests drive both paths through the same assertions so the pair cannot
drift apart.
"""

from __future__ import annotations

import pytest

from tests._native_ws import mark_accepted
from veloce.websocket import (
    WS_1005_NO_STATUS_RCVD,
    WS_1006_ABNORMAL_CLOSURE,
    WebSocket,
    WebSocketDisconnect,
)

#: The two ways into a receive: no deadline (inline) and a deadline (wrapped).
#: Every disconnect assertion below runs under both.
_BOUNDING = [
    pytest.param({}, id="unbounded"),
    pytest.param({"timeout": 5.0}, id="per-call timeout"),
]


def _socket(
    messages: list[dict],
    *,
    idle_timeout: float | None = None,
    sent: list[dict] | None = None,
) -> WebSocket:
    """An accepted ASGI socket that will deliver `messages` in order.

    `WebSocket` is slotted, so a caller that wants to see what was sent passes
    the list to append to rather than reading it back off the socket.
    """
    outbound = sent if sent is not None else []
    queue = list(messages)

    async def receive() -> dict:
        return queue.pop(0) if queue else {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict) -> None:
        outbound.append(message)

    ws = mark_accepted(
        WebSocket.from_asgi({"type": "websocket"}, receive, send, idle_timeout=idle_timeout)
    )
    return ws


def _dead_socket(exc: BaseException) -> WebSocket:
    """An accepted ASGI socket whose peer is gone: every send raises."""

    async def receive() -> dict:
        return {"type": "websocket.receive", "text": "unused"}

    async def send(message: dict) -> None:
        raise exc

    ws = mark_accepted(WebSocket.from_asgi({"type": "websocket"}, receive, send))
    return ws


# ── A message arrives ────────────────────────────────────────────────


@pytest.mark.parametrize("bounding", _BOUNDING)
async def test_a_text_message_reads_the_same_either_way(bounding):
    ws = _socket([{"type": "websocket.receive", "text": "hello"}])
    assert await ws.receive_text(**bounding) == "hello"


@pytest.mark.parametrize("bounding", _BOUNDING)
async def test_a_binary_message_reads_the_same_either_way(bounding):
    ws = _socket([{"type": "websocket.receive", "bytes": b"hi"}])
    assert await ws.receive_bytes(**bounding) == b"hi"


@pytest.mark.parametrize("bounding", _BOUNDING)
async def test_bytes_delivered_to_receive_text_are_decoded_either_way(bounding):
    ws = _socket([{"type": "websocket.receive", "bytes": "héllo".encode()}])
    assert await ws.receive_text(**bounding) == "héllo"


@pytest.mark.parametrize("bounding", _BOUNDING)
async def test_text_delivered_to_receive_bytes_is_encoded_either_way(bounding):
    ws = _socket([{"type": "websocket.receive", "text": "héllo"}])
    assert await ws.receive_bytes(**bounding) == "héllo".encode()


async def test_a_configured_idle_timeout_takes_the_bounded_path_and_still_reads():
    ws = _socket([{"type": "websocket.receive", "text": "hello"}], idle_timeout=5.0)
    assert await ws.receive_text() == "hello"


# ── The peer closes ──────────────────────────────────────────────────


@pytest.mark.parametrize("bounding", _BOUNDING)
@pytest.mark.parametrize("method", ["receive_text", "receive_bytes"])
async def test_a_disconnect_raises_and_records_the_peers_code(bounding, method):
    ws = _socket([{"type": "websocket.disconnect", "code": 1011, "reason": "boom"}])
    with pytest.raises(WebSocketDisconnect) as caught:
        await getattr(ws, method)(**bounding)
    assert caught.value.code == 1011
    assert ws.close_code == 1011
    assert ws.close_reason == "boom"
    assert ws._closed is True


@pytest.mark.parametrize("bounding", _BOUNDING)
async def test_a_disconnect_without_a_code_records_the_no_status_default(bounding):
    ws = _socket([{"type": "websocket.disconnect"}])
    with pytest.raises(WebSocketDisconnect) as caught:
        await ws.receive_text(**bounding)
    assert caught.value.code == WS_1005_NO_STATUS_RCVD
    assert ws.close_code == WS_1005_NO_STATUS_RCVD


@pytest.mark.parametrize("bounding", _BOUNDING)
async def test_a_disconnect_without_a_reason_records_an_empty_one(bounding):
    ws = _socket([{"type": "websocket.disconnect", "code": 1000, "reason": None}])
    with pytest.raises(WebSocketDisconnect):
        await ws.receive_text(**bounding)
    assert ws.close_reason == ""


async def test_the_idle_timeout_path_records_a_disconnect_identically():
    ws = _socket([{"type": "websocket.disconnect", "code": 1011, "reason": "boom"}], idle_timeout=5)
    with pytest.raises(WebSocketDisconnect) as caught:
        await ws.receive_text()
    assert (caught.value.code, ws.close_code, ws.close_reason) == (1011, 1011, "boom")


@pytest.mark.parametrize("bounding", _BOUNDING)
async def test_a_receive_after_the_peer_closed_still_raises(bounding):
    """The state guard runs before either path is chosen."""
    ws = _socket([{"type": "websocket.disconnect", "code": 1011}])
    with pytest.raises(WebSocketDisconnect):
        await ws.receive_text(**bounding)
    with pytest.raises(WebSocketDisconnect):
        await ws.receive_text(**bounding)


@pytest.mark.parametrize("bounding", _BOUNDING)
async def test_a_receive_before_accept_is_an_api_error_either_way(bounding):
    ws = _socket([{"type": "websocket.receive", "text": "hello"}])
    ws._accepted = False
    with pytest.raises(RuntimeError, match="call accept"):
        await ws.receive_text(**bounding)


# ── The peer is gone on send ─────────────────────────────────────────


@pytest.mark.parametrize("method", ["send_text", "send_bytes"])
@pytest.mark.parametrize(
    "exc", [ConnectionResetError("reset"), BrokenPipeError("pipe"), OSError("gone")]
)
async def test_a_dead_peer_send_becomes_a_disconnect(method, exc):
    """A handler catches one exception on every transport, not a socket error.

    This is the single home for send-error normalisation. A module named for
    the `WebSocketState` enum carried two hand-written cases covering one
    method and one exception each; both are subsets of this matrix, which also
    asserts the close code and the `__cause__` they did not.
    """
    ws = _dead_socket(exc)
    payload = "hi" if method == "send_text" else b"hi"
    with pytest.raises(WebSocketDisconnect) as caught:
        await getattr(ws, method)(payload)
    assert caught.value.code == WS_1006_ABNORMAL_CLOSURE
    assert caught.value.__cause__ is exc
    assert ws._closed is True


@pytest.mark.parametrize("method", ["send_text", "send_bytes"])
async def test_a_send_after_a_dead_peer_short_circuits(method):
    ws = _dead_socket(BrokenPipeError("pipe"))
    payload = "hi" if method == "send_text" else b"hi"
    with pytest.raises(WebSocketDisconnect):
        await getattr(ws, method)(payload)
    with pytest.raises(WebSocketDisconnect):
        await getattr(ws, method)(payload)


@pytest.mark.parametrize("method", ["send_text", "send_bytes"])
async def test_an_unrelated_send_error_is_not_swallowed(method):
    """Only a dead peer becomes a disconnect; anything else is the app's bug."""
    ws = _dead_socket(ValueError("something else"))
    payload = "hi" if method == "send_text" else b"hi"
    with pytest.raises(ValueError, match="something else"):
        await getattr(ws, method)(payload)
    assert ws._closed is False


@pytest.mark.parametrize("method", ["send_text", "send_bytes"])
async def test_a_send_before_accept_is_an_api_error(method):
    ws = _socket([])
    ws._accepted = False
    payload = "hi" if method == "send_text" else b"hi"
    with pytest.raises(RuntimeError, match="call accept"):
        await getattr(ws, method)(payload)


async def test_a_send_puts_the_expected_asgi_message_on_the_wire():
    sent: list[dict] = []
    ws = _socket([], sent=sent)
    await ws.send_text("hello")
    await ws.send_bytes(b"raw")
    assert sent == [
        {"type": "websocket.send", "text": "hello"},
        {"type": "websocket.send", "bytes": b"raw"},
    ]
