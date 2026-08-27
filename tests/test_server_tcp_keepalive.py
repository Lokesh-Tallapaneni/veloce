"""OS-level TCP keepalive (`SO_KEEPALIVE`) on the built-in server's sockets.

Split out of `test_server_protocol.py`, which ran to 2,112 lines across ten
protocol concerns with two section separators - both of them past line
1,667, marking exactly this seam.
"""

from __future__ import annotations

import asyncio

from tests._protocol import _drain_loop, _FakeTransport
from veloce import Veloce
from veloce.serving.protocol import (
    HttpProtocol,
)


class _FakeSocket:
    """Records setsockopt calls so keepalive tests can assert what was set."""

    def __init__(self) -> None:
        self.opts: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, optname: int, value: int) -> None:
        self.opts.append((level, optname, value))


class _KeepAliveTransport(_FakeTransport):
    """Full-duplex transport that surfaces a fake socket via get_extra_info."""

    def __init__(self, sock: object) -> None:
        super().__init__()
        self._sock = sock

    def get_extra_info(self, name: str, default: object = None) -> object:
        if name == "socket":
            return self._sock
        return default


def test_connection_made_sets_so_keepalive():
    """On the native path SO_KEEPALIVE is enabled on the accepted socket."""
    import socket

    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        sock = _FakeSocket()
        proto.connection_made(_KeepAliveTransport(sock))

        assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in sock.opts
    finally:
        loop.close()


def test_keepalive_disabled_by_config():
    """TCP_KEEPALIVE=False leaves the socket untouched."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["TCP_KEEPALIVE"] = False
        proto = HttpProtocol(app, loop)
        sock = _FakeSocket()
        proto.connection_made(_KeepAliveTransport(sock))

        assert sock.opts == []
    finally:
        loop.close()


def test_keepalive_tuning_options_are_platform_guarded():
    """Idle/interval/count are applied only where the platform exposes them.

    The values configured here are asserted against whichever of
    TCP_KEEPIDLE/TCP_KEEPALIVE, TCP_KEEPINTVL and TCP_KEEPCNT this build
    defines. On Windows none exist, so only SO_KEEPALIVE is set - the native
    run() path must not crash there.
    """
    import socket

    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["TCP_KEEPALIVE_IDLE"] = 120
        app.config["TCP_KEEPALIVE_INTERVAL"] = 30
        app.config["TCP_KEEPALIVE_COUNT"] = 5
        proto = HttpProtocol(app, loop)
        sock = _FakeSocket()
        proto.connection_made(_KeepAliveTransport(sock))

        assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in sock.opts

        idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
        if idle_opt is not None:
            assert (socket.IPPROTO_TCP, idle_opt, 120) in sock.opts
        if hasattr(socket, "TCP_KEEPINTVL"):
            assert (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30) in sock.opts
        if hasattr(socket, "TCP_KEEPCNT"):
            assert (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5) in sock.opts

        # Whatever the platform, no tuning option is set unless it exists.
        tcp_opts = {opt for level, opt, _ in sock.opts if level == socket.IPPROTO_TCP}
        available = {
            getattr(socket, name)
            for name in ("TCP_KEEPIDLE", "TCP_KEEPALIVE", "TCP_KEEPINTVL", "TCP_KEEPCNT")
            if hasattr(socket, name)
        }
        assert tcp_opts <= available
    finally:
        loop.close()


def test_keepalive_skipped_when_no_socket():
    """A transport without a backing socket (TLS/test) does not error."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        # _FakeTransport.get_extra_info returns None for "socket".
        proto.connection_made(_FakeTransport())

        assert proto.transport is not None
        assert proto._counted is True
    finally:
        loop.close()


def test_keepalive_setsockopt_failure_is_swallowed():
    """A socket that raises on setsockopt does not break connection setup."""
    loop = asyncio.new_event_loop()
    try:

        class _RaisingSocket(_FakeSocket):
            def setsockopt(self, level: int, optname: int, value: int) -> None:
                raise OSError("nope")

        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        proto.connection_made(_KeepAliveTransport(_RaisingSocket()))

        assert proto.transport is not None
        assert proto._counted is True
    finally:
        loop.close()


def test_native_server_serves_query_method_with_body():
    """The native HttpProtocol parses the QUERY method (RFC 10008) and delivers
    its body to the handler, returning the handler's response."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        @app.query("/search")
        async def search(request):  # noqa: ANN001, ANN202
            payload = await request.json()
            return {"term": payload["term"]}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        body = b'{"term":"veloce"}'
        proto.data_received(
            b"QUERY /search HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        assert emitted.startswith(b"HTTP/1.1 200")
        assert b'"term":"veloce"' in emitted
    finally:
        loop.close()
