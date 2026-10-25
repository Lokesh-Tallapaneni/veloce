"""Live server — runs the app on a real loopback socket over the native transport.

`TestClient` and `AsyncTestClient` build ASGI scopes in process, so they never
reach `veloce.serving.protocol.HttpProtocol` - the HTTP/1.1 parser, keep-alive
reuse, chunked bodies, pipelining and response framing that `Veloce.run()`
actually serves with. `LiveServer` binds an ephemeral port, serves it with that
same protocol, and speaks HTTP/1.1 back to it over a socket, so what a test
asserts on is what went over the wire.

Test-only tooling. It is reached as `veloce.LiveServer`, which resolves on first
access, so a normal `import veloce` never pays for the serving stack.

The response reader follows RFC 9112 Sec. 6: an interim `1xx` is skipped, a
`204` / `304` and the reply to a `HEAD` carry no body, `Transfer-Encoding:
chunked` frames the body in chunks, otherwise `Content-Length` bounds it and its
absence means read to end of stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import socket
import sys
import weakref
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urlencode

from typing_extensions import Doc

from veloce._constants import (
    HEADER_CONNECTION,
    HEADER_CONTENT_LENGTH,
    HEADER_HOST,
    HEADER_TRANSFER_ENCODING,
)
from veloce._protocol_constants import (
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_POST,
    LIFECYCLE_SHUTDOWN,
    LIFECYCLE_STARTUP,
    URL_SCHEME_HTTP,
)
from veloce.http.datastructures import Headers
from veloce.serving.protocol import HttpProtocol
from veloce.status import HTTP_200_OK, HTTP_204_NO_CONTENT, HTTP_304_NOT_MODIFIED
from veloce.testclient import (
    StreamBody,
    TestResponse,
    _aiter_body_chunks,
    _prepare_body_request,
)

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce

# Statuses that carry no body however they are framed (RFC 9112 Sec. 6.3), so a
# `Content-Length` on one of them must not be read as a body length.
_BODYLESS_STATUSES = frozenset((HTTP_204_NO_CONTENT, HTTP_304_NOT_MODIFIED))

_RAW_CONTENT_LENGTH = HEADER_CONTENT_LENGTH.lower().encode("ascii")
_RAW_TRANSFER_ENCODING = HEADER_TRANSFER_ENCODING.lower().encode("ascii")


def _parse_int(raw: bytes, base: int, what: str) -> int:
    """Parse a framing integer, naming what failed when the bytes are not one."""
    try:
        return int(raw, base)
    except ValueError as exc:
        raise RuntimeError(f"malformed {what} in the server's response: {raw!r}") from exc


def _parse_head(head: bytes) -> tuple[int, list[tuple[bytes, bytes]]]:
    """Split a response header block into `(status_code, raw_headers)`.

    `head` includes the terminating CRLFCRLF. Header names are lowercased so the
    framing lookups below match regardless of how the server cased them.
    """
    lines = head[:-4].split(b"\r\n")
    status_line = lines[0]
    parts = status_line.split(b" ", 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
        raise RuntimeError(f"malformed status line in the server's response: {status_line!r}")
    status_code = _parse_int(parts[1], 10, "status code")
    raw_headers: list[tuple[bytes, bytes]] = []
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        if not sep:
            raise RuntimeError(f"malformed header line in the server's response: {line!r}")
        raw_headers.append((name.strip().lower(), value.strip()))
    return status_code, raw_headers


def _find_header(raw_headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    """Return the first value for a lowercased header name, or `None`."""
    for key, value in raw_headers:
        if key == name:
            return value
    return None


def _build_target(
    path: str,
    params: dict[str, str] | Sequence[tuple[str, str]] | None,
) -> str:
    """Join a path and `params` into one request target."""
    if not params:
        return path
    query = urlencode(params)
    return f"{path}&{query}" if "?" in path else f"{path}?{query}"


def _encode_request_head(method: str, target: str, headers: Iterable[tuple[str, str]]) -> bytes:
    """Encode a request line plus header block, terminator included."""
    lines = [f"{method} {target} HTTP/1.1"]
    lines.extend(f"{name}: {value}" for name, value in headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


async def _read_chunked_body(reader: asyncio.StreamReader) -> bytes:
    """Read a `Transfer-Encoding: chunked` body, dropping the chunk framing.

    Reading each size line and then exactly that many bytes is what makes this a
    framing assertion: a server that miscounts a chunk, forgets the trailing
    CRLF, or omits the terminating zero chunk desynchronises here instead of
    quietly returning a body that happens to look right.
    """
    chunks: list[bytes] = []
    while True:
        size_line = await reader.readuntil(b"\r\n")
        # RFC 9112 Sec. 7.1: the size may be followed by chunk extensions.
        size = _parse_int(size_line.split(b";", 1)[0].strip(), 16, "chunk size")
        if size == 0:
            # The last chunk is followed by any trailer fields and a blank line.
            while await reader.readuntil(b"\r\n") != b"\r\n":
                pass
            return b"".join(chunks)
        chunks.append(await reader.readexactly(size))
        terminator = await reader.readexactly(2)
        if terminator != b"\r\n":
            raise RuntimeError(f"chunk of {size} bytes was not CRLF-terminated: {terminator!r}")


async def _read_response(reader: asyncio.StreamReader, *, body_expected: bool) -> TestResponse:
    """Read one complete HTTP/1.1 response off the socket."""
    while True:
        head = await reader.readuntil(b"\r\n\r\n")
        status_code, raw_headers = _parse_head(head)
        # RFC 9112 Sec. 6.1: an interim 1xx (the 100 answering `Expect`)
        # precedes the real response; it is framing, not the answer.
        if status_code >= HTTP_200_OK:
            break

    body = b""
    if body_expected and status_code not in _BODYLESS_STATUSES:
        transfer_encoding = _find_header(raw_headers, _RAW_TRANSFER_ENCODING)
        content_length = _find_header(raw_headers, _RAW_CONTENT_LENGTH)
        if transfer_encoding is not None and transfer_encoding.lower().endswith(b"chunked"):
            body = await _read_chunked_body(reader)
        elif content_length is not None:
            length = _parse_int(content_length, 10, "Content-Length")
            if length:
                body = await reader.readexactly(length)
        else:
            body = await reader.read()
    return TestResponse(status_code, body, raw_headers)


_logger = logging.getLogger(__name__)

#: The loopback to talk to when a server is listening on a wildcard. A wildcard
#: is an instruction to bind every interface, not an address anything can
#: connect to: advertising it produced a `base_url` no client could use, and
#: connecting to it fails outright on Windows.
_WILDCARD_LOOPBACK = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}

#: One `getaddrinfo` answer: family, socket type, protocol, canonical name and
#: the address to bind.
_AddrInfo = tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[Any, ...]]


def _connectable_host(address: str, sockname: tuple[Any, ...]) -> str:
    """Return an address a client can connect to for this bound socket.

    A wildcard becomes its loopback. A link-local address keeps the scope id the
    socket reports, because without it the address names an interface the
    kernel cannot pick (RFC 4007 Sec. 11).
    """
    loopback = _WILDCARD_LOOPBACK.get(address)
    if loopback is not None:
        return loopback
    scope = sockname[3] if len(sockname) > 3 else 0
    if scope and address.startswith("fe80:"):
        return f"{address}%{scope}"
    return address


#: Bind failures that mean "not at this address" rather than "not at all", so
#: the next resolved address is the one that can serve. A family the kernel has
#: switched off reports one of these; asyncio skips `EADDRNOTAVAIL` for the same
#: reason.
_SKIPPABLE_BIND_ERRNOS = frozenset({errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT})


def _bind_listener(infos: Sequence[_AddrInfo], host: str) -> socket.socket:
    """Bind the first resolved address that will take it, and return the socket.

    Not only the first answer: a kernel with IPv6 switched off still answers
    `::` for a passive lookup and then refuses it, and asyncio's own server
    moves on to the next address rather than failing outright. The resolution
    order is otherwise kept, because the first answer is also the first a
    client's connect will try.

    A failure that is not about the address - a port already in use, a port
    needing privilege - is raised where it happened rather than silently
    retried against an address that would fail the same way.
    """
    attempted: list[Any] = []
    last: OSError | None = None
    for family, sock_type, proto, _canon, sockaddr in infos:
        attempted.append(sockaddr)
        try:
            # Inside the loop's error handling: creating the socket is itself
            # where a disabled address family fails, before any bind, and
            # that is exactly the case the next address is here to serve.
            sock = socket.socket(family, sock_type, proto)
        except OSError as exc:
            last = exc
            continue
        try:
            # The options `create_server` would have set for us, now that the
            # socket is ours: address reuse everywhere it means "rebind a port
            # in TIME_WAIT" (on Windows it means something else and asyncio
            # does not set it either), and v6-only on an IPv6 socket bound to a
            # named address, so the bound address is the one that was asked for.
            if sys.platform != "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
                # A wildcard is an instruction to serve every interface, and one
                # socket can only do that for both families as a dual-stack one:
                # v6-only on `::` refused IPv4 outright, so `LiveServer(host="")`
                # served `::1` while `127.0.0.1` timed out.
                # `getaddrinfo` normalises every IPv6 wildcard spelling to
                # `::`, which is why one comparison covers them all.
                v6only = 0 if sockaddr[0] == "::" else 1
                # Some platforms (OpenBSD) have no dual-stack socket to ask
                # for. The bound family is still served, and `authority` reports
                # the address that reaches it.
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
            sock.bind(sockaddr)
        except OSError as exc:
            sock.close()
            last = exc
            if exc.errno not in _SKIPPABLE_BIND_ERRNOS:
                raise OSError(exc.errno, f"cannot bind {sockaddr}: {exc}") from exc
            continue
        except BaseException:
            sock.close()
            raise
        return sock
    raise OSError(
        f"cannot bind host {host!r}: no resolved address would take it, tried {attempted}"
    ) from last


#: How often teardown re-checks whether the drained connections have closed.
#: Short enough that a graceful teardown is not perceptibly delayed, long
#: enough not to spin the loop while waiting out the bound.
_DRAIN_POLL_SECONDS = 0.005


class LiveServer:
    """Serve an app on a real loopback socket for the duration of a test.

    The app runs on the native `HttpProtocol` transport - the one `Veloce.run()`
    uses - so keep-alive reuse, chunked bodies and response framing are
    exercised rather than simulated. Port `0` asks the OS for a free port;
    `port` and `base_url` report what it gave.

    Usage::

        async with LiveServer(app) as server:
            response = await server.get("/items")

        assert response.status_code == 200

    Each `get` / `post` / `request` call on the server itself uses a fresh
    connection. To send several requests down one connection - the thing the
    in-memory clients cannot do - use `connect()`::

        async with LiveServer(app) as server, server.connect() as conn:
            first = await conn.get("/items")
            second = await conn.get("/items")
    """

    __test__ = False  # don't let pytest collect this as a test class

    def __init__(
        self,
        app: Annotated[Veloce, Doc("The application to serve.")],
        host: Annotated[
            str,
            Doc(
                "Interface to bind; loopback by default. A named IPv6 address "
                "listens on IPv6 only, as asyncio's own server does. An empty "
                "host binds whichever wildcard the OS offers first, and a "
                "wildcard (`0.0.0.0`, `::`, or empty) binds every interface of "
                "its family - an IPv6 one serves IPv4 as well. `port`, "
                "`authority` and `base_url` then report the matching loopback, "
                "the address a client can actually connect to."
            ),
        ] = "127.0.0.1",
        port: Annotated[int, Doc("Port to bind; `0` asks the OS for a free one.")] = 0,
        timeout: Annotated[
            float, Doc("Seconds to wait for a complete response before failing.")
        ] = 5.0,
        shutdown_timeout: Annotated[
            float, Doc("Seconds to wait for open connections to close on teardown.")
        ] = 5.0,
    ) -> None:
        self.app = app
        self.host = host
        # Filled in by `__aenter__` from the socket it bound; see `authority`.
        self._bound_host = host
        self.timeout = timeout
        self.shutdown_timeout = shutdown_timeout
        self._requested_port = port
        self._server: asyncio.AbstractServer | None = None
        self._port = 0
        self._connections: set[_LiveConnection] = set()
        # Every protocol this server accepted, so teardown can force the ones a
        # client is still holding. Per instance rather than through
        # `HttpProtocol._live_connections`, which is class-level and would let
        # one server's teardown abort a concurrent server's sockets. Weak, like
        # that one: a strong set would pin every connection's protocol and
        # buffers for the server's whole lifetime, and a finished connection is
        # not one teardown has to force.
        self._protocols: weakref.WeakSet[HttpProtocol] = weakref.WeakSet()
        self._lifespan_run = False

    @property
    def port(self) -> int:
        """The bound TCP port. Only meaningful once the server has started."""
        self._require_started("port")
        return self._port

    @property
    def authority(self) -> str:
        """`host:port` as a URL and a `Host:` header must spell it.

        An IPv6 literal is bracketed (RFC 3986 Sec. 3.2.2), or the colons in the
        address run into the port: `::1:8080` names no host, and an app parsing
        it for `request.url` fell back to a default.
        """
        self._require_started("authority")
        host = self._bound_host
        host = f"[{host}]" if ":" in host else host
        return f"{host}:{self._port}"

    @property
    def base_url(self) -> str:
        """`http://host:port` for the bound socket."""
        return f"{URL_SCHEME_HTTP}://{self.authority}"

    def _require_started(self, what: str) -> None:
        if self._server is None:
            raise RuntimeError(
                f"LiveServer.{what} is available only inside `async with LiveServer(app) as ...`"
            )

    # ── Lifecycle ─────────────────────────────────────────

    async def __aenter__(self) -> LiveServer:
        self.app._setup_openapi()
        loop = asyncio.get_running_loop()
        # Bound to one resolved address rather than to the name. A name that
        # resolves to both families - `localhost` - made `create_server` bind
        # two sockets, and with port 0 the OS gave each a *different* port while
        # only the first was reported: a client resolving the name to the other
        # family then connected to a port nothing was listening on. The first
        # `getaddrinfo` answer is also the first a client's connect will try, so
        # binding it is the one that matches.
        # Resolved on the loop: `socket.getaddrinfo` blocks, and this runs on an
        # async path where blocking calls are not allowed.
        # `AI_PASSIVE` with no host is what asks for an address to *bind*: an
        # empty host resolved without it gave the first real interface - a
        # link-local one here - so `LiveServer(host="")` listened on one
        # interface where it reads as "all of them".
        sock = _bind_listener(
            await loop.getaddrinfo(
                self.host or None,
                self._requested_port,
                type=socket.SOCK_STREAM,
                flags=socket.AI_PASSIVE,
            ),
            self.host,
        )
        try:
            server = await loop.create_server(
                lambda: self._track(HttpProtocol(self.app, loop)), sock=sock
            )
        except BaseException:
            # The socket is bound and nothing owns it yet, so the port would be
            # held until the process ended.
            sock.close()
            raise
        self._server = server
        bound = server.sockets[0].getsockname()
        self._port = int(bound[1])
        # The address actually bound, not the name that was asked for. One name
        # can resolve to several addresses and only one of them is listening,
        # so a `base_url` built from the name is a URL that may not connect -
        # and the `Host:` header built from it named a host the app could not
        # match either.
        self._bound_host = _connectable_host(str(bound[0]), bound)
        # Bind first, then start: a failed startup hook must not leave a
        # listening socket behind, and the caller never gets the chance to
        # `__aexit__` a context manager whose entry raised.
        try:
            await self.app._run_lifecycle(LIFECYCLE_STARTUP)
        except BaseException:
            self._server = None
            server.close()
            await server.wait_closed()
            raise
        self._lifespan_run = True
        return self

    def _track(self, protocol: HttpProtocol) -> HttpProtocol:
        """Register an accepted connection so teardown can force it."""
        self._protocols.add(protocol)
        return protocol

    async def __aexit__(self, *exc: object) -> None:
        """Close the clients, the listener and the app, whatever any of them raises.

        Every phase runs even when an earlier one failed. Without that, a raise
        while closing a client socket left the listening socket bound - so the
        next test to ask for that port failed - and skipped the app's shutdown
        hooks entirely, which is the opposite of what a context manager is for.
        The original exception still propagates.
        """
        try:
            await self._close_client_connections()
        finally:
            try:
                await self._close_listener()
            finally:
                await self._run_shutdown_hooks()

    async def _close_client_connections(self) -> None:
        """Close the connections this server handed out.

        Each close is awaited until the server has shut its own end, so by the
        time the listener closes no connection of ours is live and there is
        nothing left for `wait_closed()` to sit on.
        """
        try:
            for connection in list(self._connections):
                await connection.aclose()
        finally:
            self._connections.clear()

    async def _close_listener(self) -> None:
        """Stop accepting, drain what is open, and force what will not go."""
        server = self._server
        self._server = None
        if server is None:
            return
        try:
            server.close()
            # Ask each connection to quiesce first. `begin_drain` lets an
            # in-flight request finish and closes an *idle* keep-alive socket at
            # once - the common case for a test that drove the server with an
            # external client - so teardown does not sit out the bound below
            # waiting for a connection that was never going to speak again.
            for protocol in list(self._protocols):
                protocol.begin_drain()
            # Then wait, bounded, for the drained connections to actually go,
            # watching the connections rather than the server. `wait_closed()`
            # is not that wait on every version this supports: before 3.12 it
            # returns as soon as the listener is shut, so a bound built on it
            # would silently do nothing on 3.10 and 3.11. Measured on all four.
            await self._await_connections_closed()
            # Anything a client is still holding - an unfinished in-flight
            # request, or a socket that ignored the drain - is taken out here,
            # so teardown is bounded by the timeout and not by the client.
            self._force_close()
            # The listener last, so the port is really free before it is
            # reported as such. The aborts above leave nothing to wait for.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(server.wait_closed(), self.shutdown_timeout)
        finally:
            # Even if the drain raised: a connection left in the registry is a
            # protocol, its transport and its buffers held for as long as this
            # object is, and the abort above is the only thing that frees them.
            self._force_close()
            self._protocols.clear()

    async def _run_shutdown_hooks(self) -> None:
        """Run the app's shutdown lifecycle once, if startup ran."""
        if not self._lifespan_run:
            return
        self._lifespan_run = False
        await self.app._run_lifecycle(LIFECYCLE_SHUTDOWN)

    async def _await_connections_closed(self) -> None:
        """Wait up to `shutdown_timeout` for this server's connections to close."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.shutdown_timeout
        while loop.time() < deadline:
            if not self._held():
                return
            await asyncio.sleep(_DRAIN_POLL_SECONDS)

    def _held(self) -> list[HttpProtocol]:
        """Return the connections of this server that are still open."""
        return [
            protocol
            for protocol in self._protocols
            if protocol.transport is not None and not protocol.transport.is_closing()
        ]

    def _force_close(self) -> None:
        """Abort the connections a client is still holding open."""
        held = self._held()
        if not held:
            return
        _logger.warning(
            "LiveServer: %d connection(s) still open after %.3gs; aborting them. "
            "A client that keeps a connection alive past the test - an external "
            "HTTP client, or one whose response was never read - is the usual cause.",
            len(held),
            self.shutdown_timeout,
        )
        for protocol in held:
            transport = protocol.transport
            if transport is not None:
                transport.abort()

    # ── Connections ───────────────────────────────────────

    def connect(self) -> _LiveConnection:
        """Return an unopened connection to this server - an async context manager.

        Several requests sent through one `_LiveConnection` share one TCP
        connection, which is how a test reaches keep-alive reuse and pipelining.
        """
        self._require_started("connect()")
        return _LiveConnection(self)

    # ── Requests ──────────────────────────────────────────

    async def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        """Send one request over a fresh connection and return the response.

        The body forms match `TestClient`: `json`, `data`, `content` and `files`
        are buffered, and `stream` sends the body with `Transfer-Encoding:
        chunked` instead (it excludes the other four).

        The connection closes after the answer, so this sends
        `Connection: close` and a `Connection` header passed here is replaced.
        Hold a connection open with `connect()` to decide that yourself.
        """
        async with self.connect() as connection:
            return await connection.request(
                method,
                path,
                headers=headers,
                json=json,
                data=data,
                content=content,
                files=files,
                params=params,
                stream=stream,
                close=True,
            )

    async def get(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
    ) -> TestResponse:
        """Send a GET request over a fresh connection."""
        return await self.request(HTTP_METHOD_GET, path, headers=headers, params=params)

    async def post(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        """Send a POST request over a fresh connection. See `request` for the body forms."""
        return await self.request(
            HTTP_METHOD_POST,
            path,
            headers=headers,
            json=json,
            data=data,
            content=content,
            files=files,
            params=params,
            stream=stream,
        )

    def __repr__(self) -> str:
        where = f" {self.host}:{self._port}" if self._server is not None else " (not started)"
        return f"<LiveServer{where}>"


class _LiveConnection:
    """One TCP connection to a `LiveServer`, reusable across requests.

    Returned by `LiveServer.connect()`. `send_raw` / `read_response` are the
    wire-level pair the higher-level `request` is built from; a test that needs
    to send bytes no client would generate (a malformed request line, a
    `Content-Length` the body never matches) uses them directly.
    """

    def __init__(self, server: LiveServer) -> None:
        self._server = server
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> _LiveConnection:
        # To the address the server bound, not to the name it was given: a name
        # resolving to two families would have the client try the one that is
        # not listening.
        self._reader, self._writer = await asyncio.open_connection(
            self._server._bound_host, self._server.port
        )
        self._server._connections.add(self)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection, waiting for the server to close its own end."""
        self._server._connections.discard(self)
        reader, writer = self._reader, self._writer
        self._reader = self._writer = None
        if writer is None or reader is None:
            return
        # Half-closing makes the server's transport close itself (it declares no
        # `eof_received`), so draining to end of stream is a positive signal that
        # the server side is gone - no sleeping, and no keep-alive connection
        # left for the listener's `wait_closed()` to sit on. The suppressions
        # cover a peer that has already closed, which every refusal path does.
        with contextlib.suppress(OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            if writer.can_write_eof():
                writer.write_eof()
                await asyncio.wait_for(reader.read(), self._server.timeout)
        writer.close()
        with contextlib.suppress(OSError, asyncio.TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), self._server.timeout)

    def _require_open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            raise RuntimeError(
                "the connection is not open: use `async with server.connect() as conn:`"
            )
        return self._reader, self._writer

    # ── Wire level ────────────────────────────────────────

    async def send_raw(self, data: bytes) -> None:
        """Write bytes to the socket verbatim, with no framing of their own."""
        _, writer = self._require_open()
        writer.write(data)
        # A refusal (400, 413) closes the connection as it is written, so the
        # drain can legitimately fail on a request the server has already
        # answered; the response is read next regardless.
        with contextlib.suppress(OSError):
            await writer.drain()

    async def read_response(self, *, body_expected: bool = True) -> TestResponse:
        """Read one response off the socket.

        `body_expected=False` for the reply to a `HEAD`, which carries the
        `Content-Length` of the body it is not sending.
        """
        reader, _ = self._require_open()
        timeout = self._server.timeout
        try:
            return await asyncio.wait_for(
                _read_response(reader, body_expected=body_expected), timeout
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"no complete HTTP response from {self._server.base_url} within {timeout}s"
            ) from exc

    # ── Requests ──────────────────────────────────────────

    async def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
        stream: StreamBody | None = None,
        close: bool = False,
    ) -> TestResponse:
        """Send a request on this connection and return the response.

        `close=True` asks the server to close the connection after answering,
        which is what the one-shot methods on `LiveServer` do; left `False` the
        connection stays open for the next request.
        """
        verb = method.upper()
        request_headers, body = _prepare_body_request(json, data, content, files, headers, stream)
        # `Headers` rather than a dict, because it matches a name without regard
        # to case and keeps a repeated one. A dict left a caller's lowercase
        # `host` beside the authority filled in here, and two `Host` lines is a
        # malformed request (RFC 9110 Sec. 7.2) whose first line the server
        # reads - so the header the caller sent was the one ignored.
        wire = Headers(request_headers)
        wire.setdefault(HEADER_HOST, self._server.authority)
        if close:
            wire[HEADER_CONNECTION] = "close"
        # This client owns the framing, so the header it is not using is removed
        # rather than left beside the one it sets: both on one request is
        # forbidden (RFC 9112 Sec. 6.1) and answered 400, and a `Content-Length`
        # left on a chunked body desynchronises the connection. `send_raw` is
        # the way to put deliberately malformed framing on the wire.
        if body is None:
            wire[HEADER_TRANSFER_ENCODING] = "chunked"
            wire.popall(HEADER_CONTENT_LENGTH, None)
        else:
            wire.popall(HEADER_TRANSFER_ENCODING, None)
            wire.popall(HEADER_CONTENT_LENGTH, None)
            if body:
                wire[HEADER_CONTENT_LENGTH] = str(len(body))

        head = _encode_request_head(verb, _build_target(path, params), wire.items())
        if body is None:
            await self.send_raw(head)
            async for chunk in _aiter_body_chunks(stream):
                # An empty chunk would frame as the terminating zero chunk and
                # end the body early, so it is dropped rather than sent.
                if chunk:
                    await self.send_raw(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            await self.send_raw(b"0\r\n\r\n")
        else:
            await self.send_raw(head + body)
        return await self.read_response(body_expected=verb != HTTP_METHOD_HEAD)

    async def get(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
    ) -> TestResponse:
        """Send a GET request on this connection."""
        return await self.request(HTTP_METHOD_GET, path, headers=headers, params=params)

    async def post(
        self,
        path: str,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, str] | Sequence[tuple[str, str]] | None = None,
        stream: StreamBody | None = None,
    ) -> TestResponse:
        """Send a POST request on this connection. See `request` for the body forms."""
        return await self.request(
            HTTP_METHOD_POST,
            path,
            headers=headers,
            json=json,
            data=data,
            content=content,
            files=files,
            params=params,
            stream=stream,
        )

    def __repr__(self) -> str:
        state = "open" if self._writer is not None else "closed"
        return f"<LiveServer connection {state}>"
