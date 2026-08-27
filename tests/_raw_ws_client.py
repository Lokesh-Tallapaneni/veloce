"""One raw-socket RFC 6455 client for the native WebSocket test modules.

`_RawWSClient` was written out twice, and the two copies had already diverged:
`test_websocket_native_server.py` grew `origin`, `version` and `host_header`
arguments plus close-frame support for the handshake-refusal cases, while
`test_websocket_native_dispatch.py` kept an earlier form that asserted the 101
inside `connect` and could only read text frames. A test client that differs
between modules is a client whose behaviour has to be re-read per module before
a failure means anything.

Purpose-built rather than a third-party client so the tests pin Veloce's own
framing against the spec - the accept-key GUID, masked client-to-server frames,
the server's text and close frames - without an external library's wire
behaviour standing between the assertion and the bytes.

RFC 6455 Sec. 1.3: the accept value is the base64 SHA-1 of the client key
concatenated with the GUID below. Sec. 5.1: client-to-server frames MUST be
masked. Sec. 5.2: a payload under 126 bytes uses the 7-bit length, under 65536
the 16-bit extension, above that the 64-bit one.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import struct

from veloce import status

RFC6455_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class RawWSClient:
    """A minimal RFC 6455 client over a real socket - genuine handshake + frames."""

    #: Set by `connect` from the handshake response. Declared here because
    #: assigning them in a classmethod and reading them everywhere else cost
    #: fourteen `# type: ignore[attr-defined]` comments to silence what the
    #: class can simply say.
    status_line: str
    resp_headers: dict[str, str]
    handshake_key: str

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        path: str,
        *,
        origin: str | None = None,
        version: str = "13",
        host_header: str | None = None,
    ) -> RawWSClient:
        """Perform the upgrade handshake and return the client, accepted or not.

        The status line and response headers are recorded rather than asserted:
        the refusal cases (no route, wrong version, bad Origin, bad Host) are
        about the reply that is *not* a 101. Call `assert_accepted` for the
        cases that expect one.
        """
        reader, writer = await asyncio.open_connection(host, port)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_header or f'{host}:{port}'}",
            "Upgrade: websocket",
            "Connection: keep-alive, Upgrade",
            f"Sec-WebSocket-Key: {key}",
            f"Sec-WebSocket-Version: {version}",
        ]
        if origin is not None:
            lines.append(f"Origin: {origin}")
        request = "\r\n".join(lines) + "\r\n\r\n"
        writer.write(request.encode())
        await writer.drain()

        head = await reader.readuntil(b"\r\n\r\n")
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        resp_headers: dict[str, str] = {}
        for line in header_lines:
            k, _, v = line.partition(":")
            if k:
                resp_headers[k.strip().lower()] = v.strip()
        client = cls(reader, writer)
        client.status_line = status_line
        client.resp_headers = resp_headers
        client.handshake_key = key
        return client

    def assert_accepted(self) -> None:
        """Assert the reply was a 101 carrying the accept value for our key."""
        assert "101" in self.status_line, self.status_line
        expected = base64.b64encode(
            hashlib.sha1(  # noqa: S324
                (self.handshake_key + RFC6455_GUID).encode()
            ).digest()
        ).decode()
        assert self.resp_headers.get("sec-websocket-accept") == expected

    async def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytes([0x81, 0x80 | len(payload)])
        self._writer.write(header + mask + masked)
        await self._writer.drain()

    async def send_close(self, code: int = status.WS_1000_NORMAL_CLOSURE) -> None:
        payload = struct.pack("!H", code)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytes([0x88, 0x80 | len(payload)])
        self._writer.write(header + mask + masked)
        await self._writer.drain()

    async def recv_frame(self) -> tuple[int, bytes]:
        b0 = await self._reader.readexactly(1)
        opcode = b0[0] & 0x0F
        b1 = await self._reader.readexactly(1)
        length = b1[0] & 0x7F
        if length == 126:
            length = struct.unpack("!H", await self._reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self._reader.readexactly(8))[0]
        payload = await self._reader.readexactly(length) if length else b""
        return opcode, payload

    async def recv_text(self) -> str:
        opcode, payload = await self.recv_frame()
        assert opcode == 0x1, f"expected a text frame, got opcode {opcode:#x}"
        return payload.decode("utf-8")

    async def recv_close(self) -> int:
        opcode, payload = await self.recv_frame()
        assert opcode == 0x8, f"expected a close frame, got opcode {opcode:#x}"
        return struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 0

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()
