"""An outbound close frame is legal on whichever transport sends it.

RFC 6455 Sec. 5.5 caps a control frame at 125 bytes, so a close carries a
2-byte code and at most 123 bytes of reason; Sec. 7.4.1 reserves 1005, 1006 and
1015 for local use and forbids them from appearing on the wire.

The raw branch of `close()` clamped the reason and the ASGI branch did not, and
neither checked the code. So the same `close(reason=...)` call closed cleanly
under the built-in server and, under an ASGI server whose library rejects an
over-long control frame, dropped the socket - the peer saw an abnormal `1006`
instead of the graceful close the handler asked for. `close(code=1005)` put a
reserved code straight on the wire.

Both are normalised above the transport branch now. The code is *coerced*, not
rejected: `close()` runs on the teardown path where `Veloce._run_websocket`
suppresses exceptions, so raising would skip the close entirely and produce the
very 1006 this avoids.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from veloce.status import WS_1000_NORMAL_CLOSURE
from veloce.websocket import WebSocket

_KEY = {"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="}
_MAX_CONTROL_PAYLOAD = 125


class _Transport:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def writelines(self, data) -> None:
        self.writes.extend(data)

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        pass


async def _raw_close(code: int = WS_1000_NORMAL_CLOSURE, reason: str = "") -> bytes:
    """Close on the raw transport and return the frame that went out."""
    ws = WebSocket(_Transport(), dict(_KEY))
    ws.transport = transport = _Transport()
    # Pretend the peer started the close so no reply is awaited.
    ws._peer_closed = True
    await ws.close(code=code, reason=reason)
    return b"".join(transport.writes)


async def _asgi_close(code: int = WS_1000_NORMAL_CLOSURE, reason: str = "") -> dict:
    """Close on the ASGI transport and return the message that went out."""
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "websocket.connect"}

    async def send(message: dict) -> None:
        sent.append(message)

    ws = WebSocket.from_asgi({"type": "websocket"}, receive, send)
    await ws.close(code=code, reason=reason)
    return sent[-1]


def _payload(frame: bytes) -> bytes:
    """The close frame's payload, minus the 2-byte header."""
    return frame[2:]


# ── The reason fits the control-frame budget on both transports ──────


async def test_a_long_reason_is_clamped_on_the_raw_transport():
    frame = await _raw_close(reason="R" * 300)
    # The payload is the 2-byte code plus the reason, and must fit the budget.
    assert len(_payload(frame)) <= _MAX_CONTROL_PAYLOAD


async def test_a_long_reason_is_clamped_on_the_asgi_transport():
    """The defect: this branch sent the reason whole and the peer saw 1006."""
    message = await _asgi_close(reason="R" * 300)
    assert len(message["reason"].encode("utf-8")) <= 123


async def test_both_transports_clamp_to_the_same_reason():
    raw = _payload(await _raw_close(reason="R" * 300))[2:]
    asgi = (await _asgi_close(reason="R" * 300))["reason"].encode("utf-8")
    assert raw == asgi


async def test_a_clamped_reason_stays_valid_utf8():
    """Truncation walks back to a codepoint boundary, not a byte one."""
    # Each `é` is two bytes, so a 123-byte cut lands mid-codepoint.
    message = await _asgi_close(reason="é" * 200)
    reason = message["reason"]
    assert reason.encode("utf-8").decode("utf-8") == reason
    assert len(reason.encode("utf-8")) <= 123


async def test_a_short_reason_is_sent_unchanged():
    assert (await _asgi_close(reason="bye"))["reason"] == "bye"


# ── A reserved code never reaches the wire ───────────────────────────


@pytest.mark.parametrize("code", [1005, 1006, 1015, 999, 5000])
async def test_a_code_that_may_not_appear_on_the_wire_is_coerced(code):
    """The defect: `close(code=1005)` put a reserved code on the wire."""
    frame = await _raw_close(code=code)
    assert struct.unpack("!H", _payload(frame)[:2])[0] == WS_1000_NORMAL_CLOSURE
    assert (await _asgi_close(code=code))["code"] == WS_1000_NORMAL_CLOSURE


@pytest.mark.parametrize("code", [1000, 1001, 1008, 1011, 3000, 4001, 4999])
async def test_a_legal_code_is_sent_unchanged(code):
    frame = await _raw_close(code=code)
    assert struct.unpack("!H", _payload(frame)[:2])[0] == code
    assert (await _asgi_close(code=code))["code"] == code


async def test_coercing_a_bad_code_does_not_raise():
    """`close` runs during teardown; a raise there would skip the close."""
    assert await asyncio.wait_for(_asgi_close(code=1006), timeout=1)
