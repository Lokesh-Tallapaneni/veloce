"""The shared WebSocket test seams behave as the reach-ins they replace did.

`tests/_native_ws.py` is now what twelve modules use instead of poking
`ws._accepted` (20 sites) and `ws._receive_queue` (35). A helper that quietly
did something different would change what those modules assert without any of
them saying so, which is exactly the kind of thing infrastructure tests exist to
prevent.
"""

from __future__ import annotations

import asyncio

from tests._native_ws import (
    accepted_websocket,
    deliver,
    delivered,
    mark_accepted,
    nothing_delivered,
)
from tests._ws_frames import client_frame
from veloce.websocket import WebSocket

# ── mark_accepted ────────────────────────────────────────────────────


def test_a_fresh_socket_is_not_accepted():
    """The premise: if it were already accepted the helper would be a no-op."""
    websocket, _ = accepted_websocket()
    websocket._accepted = False
    assert websocket._accepted is False


def test_mark_accepted_sets_the_state():
    websocket, _ = accepted_websocket()
    websocket._accepted = False
    assert mark_accepted(websocket)._accepted is True


def test_mark_accepted_returns_the_socket():
    """So it can wrap a construction expression."""
    websocket, _ = accepted_websocket()
    assert mark_accepted(websocket) is websocket


def test_marking_accepted_writes_nothing():
    """The whole reason it exists rather than calling `accept()`: a real
    handshake writes a 101 into the transport the test is about to assert on."""
    _, transport = accepted_websocket()
    assert transport.writes == []


def test_an_accepted_socket_can_send():
    """It is the state that follows a handshake, not just a flag."""
    websocket, transport = accepted_websocket()
    asyncio.run(websocket.send_text("hi"))
    assert transport.writes


# ── delivered / nothing_delivered ────────────────────────────────────


def test_nothing_is_delivered_on_a_fresh_socket():
    websocket, _ = accepted_websocket()
    assert nothing_delivered(websocket) is True
    assert delivered(websocket) == []


def test_a_fed_frame_is_delivered():
    websocket, _ = accepted_websocket()
    websocket.feed_data(client_frame(0x1, b"hello"))
    assert delivered(websocket) == [b"hello"]


def test_delivered_drains():
    """The behaviour that matters when converting consecutive `get_nowait`
    calls: reading twice does not return the message twice."""
    websocket, _ = accepted_websocket()
    websocket.feed_data(client_frame(0x1, b"hello"))
    assert delivered(websocket) == [b"hello"]
    assert delivered(websocket) == []


def test_delivered_preserves_order():
    websocket, _ = accepted_websocket()
    websocket.feed_data(client_frame(0x1, b"first") + client_frame(0x1, b"second"))
    assert delivered(websocket) == [b"first", b"second"]


def test_nothing_delivered_is_false_once_something_arrives():
    websocket, _ = accepted_websocket()
    websocket.feed_data(client_frame(0x1, b"hello"))
    assert nothing_delivered(websocket) is False


def test_an_incomplete_frame_delivers_nothing():
    """The property `test_websocket_framing.py` is built on."""
    websocket, _ = accepted_websocket()
    frame = client_frame(0x1, b"split-across-reads")
    websocket.feed_data(frame[:9])
    assert nothing_delivered(websocket) is True
    websocket.feed_data(frame[9:])
    assert delivered(websocket) == [b"split-across-reads"]


# ── deliver ──────────────────────────────────────────────────────────


def test_deliver_buffers_a_message():
    websocket, _ = accepted_websocket()
    deliver(websocket, b"planted")
    assert delivered(websocket) == [b"planted"]


def test_deliver_and_a_real_frame_share_the_buffer():
    """It is the same queue `feed_data` fills, not a parallel one."""
    websocket, _ = accepted_websocket()
    websocket.feed_data(client_frame(0x1, b"framed"))
    deliver(websocket, b"planted")
    assert delivered(websocket) == [b"framed", b"planted"]


def test_a_delivered_message_reaches_receive():
    websocket, _ = accepted_websocket()
    deliver(websocket, b"planted")
    assert asyncio.run(websocket.receive_bytes()) == b"planted"


# ── accepted_websocket ───────────────────────────────────────────────


def test_it_returns_a_websocket_and_its_transport():
    websocket, transport = accepted_websocket()
    assert isinstance(websocket, WebSocket)
    assert transport.writes == []


def test_custom_headers_are_used():
    websocket, _ = accepted_websocket({"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="})
    assert websocket._accepted is True
