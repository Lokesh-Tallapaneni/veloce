"""Property / fuzz tests for the radix router and request parsers (R3).

These feed many randomised inputs at the router, the multipart parser,
the WebSocket frame parser, and the cookie / query-string parsers,
asserting robustness invariants (no unhandled crash; round-trip
correctness) rather than specific values. A fixed seed keeps any
failure reproducible.
"""

from __future__ import annotations

import contextlib
import random
import string
import struct

from veloce import Veloce, WebSocket
from veloce.exceptions import BadRequest, RequestEntityTooLarge, WebSocketDisconnect
from veloce.http.cookies import parse_cookie
from veloce.http.datastructures import QueryParams, parse_multipart_form

_SEED = 20260522
_SEGMENT_CHARS = string.ascii_lowercase + string.digits + "-_"


def _rand_segment(rnd: random.Random) -> str:
    return "".join(rnd.choice(_SEGMENT_CHARS) for _ in range(rnd.randint(1, 8)))


# ── Router ────────────────────────────────────────────────────────────


def test_fuzz_router_registered_routes_always_match():
    """Every static path registered must match straight back to a route."""
    rnd = random.Random(_SEED)
    app = Veloce(openapi_url=None)

    async def handler():
        return {}

    paths: list[str] = []
    for _ in range(300):
        depth = rnd.randint(1, 5)
        path = "/" + "/".join(_rand_segment(rnd) for _ in range(depth))
        if path in paths:
            continue
        paths.append(path)
        app.add_route(path, handler, ["GET"])

    for path in paths:
        assert app.match("GET", path) is not None, f"registered path lost: {path}"


def test_fuzz_router_random_paths_never_crash():
    """Matching arbitrary junk paths returns a match or None — never raises."""
    rnd = random.Random(_SEED + 1)
    app = Veloce(openapi_url=None)

    async def h():
        return {}

    app.add_route("/users/{uid}/items/{iid}", h, ["GET"])
    app.add_route("/static/{path:path}", h, ["GET"])

    for _ in range(2000):
        segments = rnd.randint(0, 6)
        path = "/" + "/".join(
            "".join(rnd.choice(_SEGMENT_CHARS + "/.%") for _ in range(rnd.randint(0, 6)))
            for _ in range(segments)
        )
        app.match("GET", path)  # must not raise


# ── Multipart parser ──────────────────────────────────────────────────


def test_fuzz_multipart_arbitrary_bytes_never_crash():
    """Random bytes yield a FormData or the controlled RequestEntityTooLarge
    — never an unhandled parser error."""
    rnd = random.Random(_SEED + 2)
    for _ in range(500):
        body = bytes(rnd.randint(0, 255) for _ in range(rnd.randint(0, 400)))
        content_type = "multipart/form-data; boundary=" + _rand_segment(rnd)
        try:
            result = parse_multipart_form(body, content_type)
        except RequestEntityTooLarge:
            continue  # controlled DoS-cap rejection — acceptable
        assert result is not None


def test_fuzz_multipart_corrupted_valid_body_never_crash():
    """Flip random bytes in a well-formed multipart body — still no crash."""
    rnd = random.Random(_SEED + 3)
    boundary = "veloceboundary"
    base = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="field"\r\n\r\n'
        "value\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    content_type = f"multipart/form-data; boundary={boundary}"
    for _ in range(500):
        corrupted = bytearray(base)
        for _ in range(rnd.randint(1, 5)):
            corrupted[rnd.randrange(len(corrupted))] = rnd.randint(0, 255)
        # A controlled rejection (DoS cap or malformed UTF-8) is fine;
        # anything else (crash, infinite loop, silent corruption) is a bug.
        with contextlib.suppress(RequestEntityTooLarge, BadRequest):
            parse_multipart_form(bytes(corrupted), content_type)


# ── Cookie + query-string parsers ─────────────────────────────────────


def test_fuzz_cookie_parser_never_crashes():
    rnd = random.Random(_SEED + 4)
    for _ in range(2000):
        raw = "".join(rnd.choice(string.printable) for _ in range(rnd.randint(0, 60)))
        assert isinstance(parse_cookie(raw), dict)


def test_fuzz_query_parser_never_crashes():
    rnd = random.Random(_SEED + 5)
    alphabet = string.ascii_letters + string.digits + "=&%+;.- "
    for _ in range(2000):
        raw = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 60)))
        QueryParams.from_query_string(raw)  # must not raise


# ── WebSocket frame parser ────────────────────────────────────────────


class _NullTransport:
    """Minimal transport that swallows the parser's outgoing frames.

    `_parse_frame` answers pings with a pong and closes on a too-big /
    malformed frame, both of which write to the transport. A live socket
    is irrelevant to parser robustness, so the writes go nowhere.
    """

    def write(self, data: bytes) -> None:
        pass

    def writelines(self, data) -> None:
        pass

    def close(self) -> None:
        pass


def _raw_websocket() -> WebSocket:
    """Build a raw-mode WebSocket whose frame parser can run headlessly."""
    return WebSocket(_NullTransport(), headers={})


def _feed(ws: WebSocket, chunk: bytes) -> None:
    """Drive `feed_data`, absorbing the close raised by a 0x8 frame.

    A close frame raising `WebSocketDisconnect` is a controlled outcome,
    not a parser crash — the test cares only that nothing else escapes.
    """
    with contextlib.suppress(WebSocketDisconnect):
        ws.feed_data(chunk)


def _client_text_frame(payload: bytes, mask: bytes) -> bytes:
    """Encode a FIN=1 masked client text frame (RFC 6455 §5.2)."""
    n = len(payload)
    header = bytearray()
    header.append(0x80 | 0x1)  # FIN + text opcode
    if n < 126:
        header.append(0x80 | n)  # MASK bit + 7-bit length
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", n))
    header.extend(mask)
    masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return bytes(header) + masked


def test_fuzz_ws_parser_arbitrary_bytes_never_crash():
    """Arbitrary byte runs fed in random chunks never raise an unhandled
    error: the parser buffers, consumes, or triggers a controlled close."""
    rnd = random.Random(_SEED + 6)
    for _ in range(2000):
        ws = _raw_websocket()
        data = bytes(rnd.randint(0, 255) for _ in range(rnd.randint(0, 400)))
        pos = 0
        while pos < len(data) and not ws._closed:
            step = rnd.randint(1, 7)
            _feed(ws, data[pos : pos + step])
            pos += step
        # The buffer must never retain more than one not-yet-complete
        # frame's worth of bytes — a runaway buffer is an over-allocation
        # bug. (A controlled close empties / freezes it.)
        assert len(ws._recv_buffer) <= len(data)


def test_fuzz_ws_parser_split_frames_never_crash():
    """A well-formed frame fed one byte at a time, or in random splits,
    reassembles without crashing and exercises the partial-read returns."""
    rnd = random.Random(_SEED + 7)
    for _ in range(500):
        payload = bytes(rnd.randint(0, 255) for _ in range(rnd.randint(0, 130)))
        mask = bytes(rnd.randint(0, 255) for _ in range(4))
        frame = _client_text_frame(payload, mask)

        # One byte at a time — every early-return-0 branch (n<2, n<4,
        # n<offset+4, n<frame_len) fires before the final byte completes.
        ws = _raw_websocket()
        for i in range(len(frame)):
            _feed(ws, frame[i : i + 1])
        assert not ws._closed
        assert ws._recv_buffer == bytearray()
        assert ws._receive_queue.get_nowait() == payload

        # Random-sized splits of the same frame.
        ws = _raw_websocket()
        pos = 0
        while pos < len(frame):
            step = rnd.randint(1, 5)
            _feed(ws, frame[pos : pos + step])
            pos += step
        assert ws._receive_queue.get_nowait() == payload


def test_fuzz_ws_parser_corrupted_valid_frame_never_crash():
    """Flip random bytes (length / opcode / mask fields included) in a
    valid frame — only controlled outcomes, no crash, no over-allocation."""
    rnd = random.Random(_SEED + 8)
    base = _client_text_frame(b"the quick brown fox", b"\x01\x02\x03\x04")
    for _ in range(2000):
        corrupted = bytearray(base)
        for _ in range(rnd.randint(1, 5)):
            corrupted[rnd.randrange(len(corrupted))] = rnd.randint(0, 255)
        ws = _raw_websocket()
        _feed(ws, bytes(corrupted))
        # A declared 64-bit length past MAX_FRAME_SIZE must trip the
        # controlled close, never park unbounded bytes in the buffer.
        assert len(ws._recv_buffer) <= len(corrupted)


def test_fuzz_ws_parser_inflated_length_closes_not_allocates():
    """A frame declaring a 64-bit payload length beyond MAX_FRAME_SIZE
    closes the connection rather than buffering toward that length."""
    header = bytes([0x82, 127]) + struct.pack("!Q", WebSocket.MAX_FRAME_SIZE + 1)
    ws = _raw_websocket()
    _feed(ws, header)
    assert ws._closed
    assert len(ws._recv_buffer) <= len(header)
