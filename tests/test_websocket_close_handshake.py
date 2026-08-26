"""The RFC 6455 close handshake: what the bounded wait is actually for.

A server-initiated `close()` sends its close frame and then waits for the peer's
reply before dropping the transport (RFC 6455 Sec. 7.1.1), bounded by
`CLOSE_HANDSHAKE_TIMEOUT`. A peer-initiated close does not wait - the peer
already spoke.

**Nothing tested any of that.** Twenty-five tests drove a raw socket through a
fake transport with no peer to reply, so each one blocked the full five seconds
incidentally - 125 seconds of the suite, 93% of the websocket modules' runtime -
while asserting something unrelated. Grepping the suite for
`CLOSE_HANDSHAKE_TIMEOUT` found no reference at all: the wait was paid for
everywhere and verified nowhere.

The suite now shortens the timeout (see `conftest._short_close_handshake_timeout`)
and this module covers the behaviour deterministically instead: it drives the
peer's reply rather than waiting for a clock.
"""

from __future__ import annotations

import asyncio
import struct
import time

import pytest

from veloce.websocket import WebSocket

_KEY = {"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="}


class _Transport:
    """A fake asyncio transport recording what the server wrote."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    def writelines(self, buffers) -> None:
        self.writes.append(b"".join(bytes(b) for b in buffers))

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


def _socket() -> tuple[WebSocket, _Transport]:
    transport = _Transport()
    ws = WebSocket(transport, dict(_KEY))
    ws._accepted = True
    return ws, transport


def _client_close_frame(code: int = 1000) -> bytes:
    """A masked close frame, as a client must send (RFC 6455 Sec. 5.1)."""
    payload = struct.pack("!H", code)
    mask = b"\x01\x02\x03\x04"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return b"\x88" + bytes([0x80 | len(payload)]) + mask + masked


def _close_frames(transport: _Transport) -> list[bytes]:
    return [w for w in transport.writes if w and w[0] == 0x88]


# ── a server-initiated close waits for the peer ──────────────────────


async def test_a_server_initiated_close_sends_a_close_frame():
    ws, transport = _socket()
    await ws.close()
    assert len(_close_frames(transport)) == 1


async def test_a_server_initiated_close_waits_for_the_peer_reply():
    """The wait exists so both sides agree before the socket drops.

    Driven, not timed: the close is started, the peer's reply is fed in, and the
    close is asserted to have completed - so this proves the wait *ends on the
    reply* rather than on the clock.
    """
    ws, transport = _socket()
    closing = asyncio.ensure_future(ws.close())
    for _ in range(3):
        await asyncio.sleep(0)
    assert not closing.done(), "close() returned without waiting for the peer"

    ws.feed_data(_client_close_frame())
    await asyncio.wait_for(closing, timeout=1.0)
    assert transport.closed


async def test_the_wait_is_bounded_when_the_peer_never_replies():
    """A silent peer must not hold the connection open forever."""
    ws, transport = _socket()
    started = time.monotonic()
    await asyncio.wait_for(ws.close(), timeout=2.0)
    elapsed = time.monotonic() - started

    assert transport.closed
    # The suite shortens the bound; what matters is that it *is* bounded and the
    # close returned well inside the outer `wait_for`.
    assert elapsed < 1.0


# ── a peer-initiated close does not wait ─────────────────────────────


async def test_a_peer_initiated_close_does_not_wait():
    """The peer already spoke, so there is nothing to wait for."""
    ws, transport = _socket()
    ws._peer_closed = True

    closing = asyncio.ensure_future(ws.close())
    await asyncio.wait_for(closing, timeout=0.5)
    assert transport.closed


async def test_a_peer_close_frame_unblocks_a_pending_close():
    """The frame parser sets the event the close is parked on."""
    ws, _transport = _socket()
    closing = asyncio.ensure_future(ws.close())
    await asyncio.sleep(0)
    ws.feed_data(_client_close_frame(1001))
    await asyncio.wait_for(closing, timeout=1.0)
    assert closing.done()


# ── and the close is still idempotent ────────────────────────────────
#
# The negatives: a change that made close() return early, or send twice, would
# satisfy the timing assertions above.


async def test_closing_twice_sends_one_close_frame():
    ws, transport = _socket()
    ws._peer_closed = True
    await ws.close()
    await ws.close()
    assert len(_close_frames(transport)) == 1


async def test_the_close_code_reaches_the_frame():
    ws, transport = _socket()
    ws._peer_closed = True
    await ws.close(code=1001)
    frame = _close_frames(transport)[0]
    assert struct.unpack("!H", frame[2:4])[0] == 1001


async def test_the_transport_is_closed_either_way():
    for peer_started in (True, False):
        ws, transport = _socket()
        ws._peer_closed = peer_started
        await asyncio.wait_for(ws.close(), timeout=2.0)
        assert transport.closed, peer_started


def test_the_production_timeout_is_still_five_seconds():
    """The suite shortens this per test; the shipped default must not drift.

    Read off the class rather than the patched instance attribute, so the
    fixture that speeds the suite up cannot quietly become the product default.
    """
    import inspect

    source = inspect.getsource(WebSocket)
    assert "CLOSE_HANDSHAKE_TIMEOUT = 5.0" in source


@pytest.mark.parametrize("code", [1000, 1001, 1011])
async def test_every_close_code_completes_the_handshake(code):
    ws, transport = _socket()
    closing = asyncio.ensure_future(ws.close(code=code))
    await asyncio.sleep(0)
    ws.feed_data(_client_close_frame())
    await asyncio.wait_for(closing, timeout=1.0)
    assert transport.closed
