"""WebSocket receive with a timeout.

`receive_text`, `receive_bytes` and `receive_json` each take a `timeout`, and
only one of the three had a test - a single case asserting that waiting on an
empty queue eventually raises. That left the parts that matter unpinned: that a
timeout does *not* fire when data is already there, that it does not consume or
corrupt a message, that a socket survives a timeout and can still be read, and
that the refusal to read before `accept()` still comes first.

A timeout on a read is a resource control - it is what stops an idle peer
holding a connection open indefinitely - so the negative direction (it fires) and
the positive direction (it does not fire when it should not) are both load
bearing.

The socket is driven directly rather than over a transport: these are properties
of the receive queue and the state check, and a real handshake would add framing
concerns that `tests/test_websocket_*.py` already cover elsewhere.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce.websocket import WebSocket


class FakeTransport:
    """Enough transport for a socket that is never written to."""

    def __init__(self) -> None:
        self.closed = False

    def write(self, data: bytes) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def get_extra_info(self, key: str) -> None:
        return None


def _accepted_socket() -> WebSocket:
    """A socket in the state a completed handshake leaves behind.

    `accept()` is skipped deliberately - it would write a real 101 to the
    transport - and `_accepted` is set to mirror the post-handshake state the
    receive methods check for.
    """
    ws = WebSocket(FakeTransport(), {"sec-websocket-key": "test"})
    ws._accepted = True
    return ws


async def _queue(ws: WebSocket, payload: object) -> None:
    """Put a message on the native receive queue.

    The queue carries the decoded payload itself - a `str` for text, `bytes` for
    binary - not an ASGI envelope. `receive_json` decodes the text it finds.
    """
    await ws._receive_queue.put(payload)


# ── the timeout fires when nothing arrives ───────────────────────────


async def test_receive_text_times_out_on_an_idle_socket():
    with pytest.raises(asyncio.TimeoutError):
        await _accepted_socket().receive_text(timeout=0.01)


async def test_receive_bytes_times_out_on_an_idle_socket():
    with pytest.raises(asyncio.TimeoutError):
        await _accepted_socket().receive_bytes(timeout=0.01)


async def test_receive_json_times_out_on_an_idle_socket():
    with pytest.raises(asyncio.TimeoutError):
        await _accepted_socket().receive_json(timeout=0.01)


async def test_a_timeout_is_raised_rather_than_returning_none():
    """A silent `None` would read as an empty message, not as no message."""
    ws = _accepted_socket()
    with pytest.raises(asyncio.TimeoutError):
        result = await ws.receive_text(timeout=0.01)
        assert result is None  # unreachable; here so a silent pass is visible


# ── the timeout does not fire when a message is waiting ──────────────


async def test_a_queued_text_message_is_returned_before_the_timeout():
    """The positive direction: a timeout must not cost a ready message."""
    ws = _accepted_socket()
    await _queue(ws, "hello")
    assert await ws.receive_text(timeout=5) == "hello"


async def test_a_queued_binary_message_is_returned():
    ws = _accepted_socket()
    await _queue(ws, b"\x00\x01")
    assert await ws.receive_bytes(timeout=5) == b"\x00\x01"


async def test_a_queued_json_message_is_decoded():
    ws = _accepted_socket()
    await _queue(ws, '{"a": 1}')
    assert await ws.receive_json(timeout=5) == {"a": 1}


async def test_no_timeout_argument_waits_indefinitely_for_a_message():
    """`timeout=None` is the default and must not impose one."""
    ws = _accepted_socket()

    async def deliver() -> None:
        await asyncio.sleep(0.02)
        await _queue(ws, "late")

    task = asyncio.create_task(deliver())
    assert await ws.receive_text() == "late"
    await task


async def test_a_message_arriving_within_the_window_is_returned():
    ws = _accepted_socket()

    async def deliver() -> None:
        await asyncio.sleep(0.01)
        await _queue(ws, "just in time")

    task = asyncio.create_task(deliver())
    assert await ws.receive_text(timeout=5) == "just in time"
    await task


# ── a timeout leaves the socket usable ───────────────────────────────


async def test_a_socket_can_be_read_again_after_a_timeout():
    """A timeout is not a fatal state - the peer may simply have been slow."""
    ws = _accepted_socket()
    with pytest.raises(asyncio.TimeoutError):
        await ws.receive_text(timeout=0.01)

    await _queue(ws, "after")
    assert await ws.receive_text(timeout=5) == "after"


async def test_a_timeout_consumes_no_message():
    """Two messages queued after a timeout must both still arrive, in order."""
    ws = _accepted_socket()
    with pytest.raises(asyncio.TimeoutError):
        await ws.receive_text(timeout=0.01)

    await _queue(ws, "first")
    await _queue(ws, "second")
    assert await ws.receive_text(timeout=5) == "first"
    assert await ws.receive_text(timeout=5) == "second"


async def test_repeated_timeouts_do_not_accumulate_state():
    ws = _accepted_socket()
    for _ in range(5):
        with pytest.raises(asyncio.TimeoutError):
            await ws.receive_text(timeout=0.005)
    await _queue(ws, "still here")
    assert await ws.receive_text(timeout=5) == "still here"


# ── the accept check comes first ─────────────────────────────────────


async def test_reading_before_accept_is_refused_rather_than_timing_out():
    """The state error must win: a socket that was never accepted has not
    'timed out waiting', it was used wrongly."""
    ws = WebSocket(FakeTransport(), {"sec-websocket-key": "test"})
    with pytest.raises(RuntimeError):
        await ws.receive_text(timeout=0.01)


async def test_reading_bytes_before_accept_is_refused():
    ws = WebSocket(FakeTransport(), {"sec-websocket-key": "test"})
    with pytest.raises(RuntimeError):
        await ws.receive_bytes(timeout=0.01)


async def test_reading_json_before_accept_is_refused():
    ws = WebSocket(FakeTransport(), {"sec-websocket-key": "test"})
    with pytest.raises(RuntimeError):
        await ws.receive_json(timeout=0.01)


# ── the timeout argument itself ──────────────────────────────────────


async def test_a_zero_timeout_does_not_wait():
    ws = _accepted_socket()
    with pytest.raises(asyncio.TimeoutError):
        await ws.receive_text(timeout=0)


async def test_a_zero_timeout_times_out_even_with_a_ready_message():
    """`timeout=0` is not "poll without waiting".

    `asyncio.wait_for` with a zero deadline cancels the inner await before it is
    ever scheduled, so a message already on the queue is not collected. Pinned
    because "check without blocking" is the natural reading of `timeout=0`, and
    it is the wrong one - use a small positive timeout for that.
    """
    ws = _accepted_socket()
    await _queue(ws, "ready")
    with pytest.raises(asyncio.TimeoutError):
        await ws.receive_text(timeout=0)


async def test_a_small_positive_timeout_collects_a_ready_message():
    """The form that does what `timeout=0` looks like it should."""
    ws = _accepted_socket()
    await _queue(ws, "ready")
    assert await ws.receive_text(timeout=0.5) == "ready"
