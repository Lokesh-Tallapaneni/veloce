"""The WebSocket upgrade half of `HttpProtocol` (RFC 6455 Sec. 4.2)."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING

import veloce.status as status
from veloce._internal import _extract_host, _ws_handshake_rejection
from veloce._protocol_constants import (
    ROUTE_METHOD_WEBSOCKET,
)
from veloce.serving.protocol._host import ProtocolHost
from veloce.websocket import WebSocket, compute_accept

if TYPE_CHECKING:  # pragma: no cover
    pass
from veloce.serving.protocol._constants import (
    _WS_HEADER_CONNECTION,
    _WS_HEADER_HOST,
    _WS_HEADER_KEY,
    _WS_HEADER_ORIGIN,
    _WS_HEADER_UPGRADE,
    _WS_HEADER_VERSION,
    _WS_SUPPORTED_VERSION,
)

_logger = logging.getLogger(__name__)


class _WsUpgradeMixin(ProtocolHost):
    """WebSocket upgrade handling, composed into `HttpProtocol`."""

    __slots__ = ()

    def _handle_websocket_upgrade(self) -> bool:
        """Attempt the RFC 6455 handshake; return True if the connection diverted.

        True once handled, whether diverted or terminated with an HTTP error;
        False means "not an upgrade - fall through to the HTTP path".
        """
        # RFC 6455 Sec. 4.1 requires a GET, so h2c and the rest fall through to HTTP.
        if self.parser.get_method() != b"GET":
            return False
        is_ws_upgrade, ws_key, ws_version, host, origin = self._scan_ws_headers()
        if not is_ws_upgrade:
            return False

        transport = self.transport
        if transport is None or transport.is_closing():
            return True

        # RFC 6455 Sec. 4.2.2: the wrong version draws a 426 advertising ours.
        if ws_version != _WS_SUPPORTED_VERSION:
            self._write_ws_http_error(
                status.HTTP_426_UPGRADE_REQUIRED,
                b"Upgrade Required",
                extra=b"Sec-WebSocket-Version: 13\r\n",
            )
            return True

        # Without the key (RFC 6455 Sec. 4.2.1) there is no nonce for the 101.
        if ws_key is None:
            self._write_ws_http_error(status.HTTP_400_BAD_REQUEST, b"Bad Request")
            return True

        path, query_bytes = self._parse_request_target()

        # Before the route table, or matching tells an untrusted origin a path
        # exists. Pre-101, so the refusal is an HTTP 403.
        host_str = _extract_host(host.decode("latin-1")) if host else ""
        origin_str = origin.decode("latin-1") if origin is not None else ""
        if _ws_handshake_rejection(self.app._middlewares, host_str, origin_str):
            self._write_ws_http_error(status.HTTP_403_FORBIDDEN, b"Forbidden")
            return True

        # Matched BEFORE the 101, so no protocol switch on an unhandled path.
        ws_match = self.app.match(ROUTE_METHOD_WEBSOCKET, path)
        if ws_match is None:
            self._write_ws_http_error(status.HTTP_404_NOT_FOUND, b"Not Found")
            return True

        # Synchronous, so the stream switches (RFC 6455 Sec. 4.2.2) before the next read.
        accept_key = compute_accept(ws_key.decode("latin-1"))
        transport.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept_key.encode("ascii") + b"\r\n\r\n"
        )

        # Mirrors the ASGI websocket scope, so the same accessors work on both.
        headers_dict = {
            name.decode("latin-1"): value.decode("latin-1") for name, value in self.headers
        }
        scope = {
            "type": "websocket",
            "path": path,
            "query_string": query_bytes,
            "headers": self.headers,
            "client": transport.get_extra_info("peername"),
        }
        idle_timeout = self.app.config.get("WEBSOCKET_IDLE_TIMEOUT")
        ws = WebSocket.from_transport(
            transport,
            headers_dict,
            scope,
            path_params=ws_match.path_params,
            idle_timeout=idle_timeout,
        )
        # Backpressure: a slow reader suspends the handler, not the write buffer.
        ws.set_send_drain(self.drain)
        ws.set_send_gate(self._can_write.is_set)

        self._websocket = ws
        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
            self._keep_alive_handle = None
        if self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None
        self._reset_header_state()

        # Held in `_ws_task` so connection_lost can cancel it if the client drops.
        self._ws_task = self.loop.create_task(self.app._run_websocket(ws, ws_match.route_info))
        self._active_tasks.add(self._ws_task)
        self._ws_task.add_done_callback(functools.partial(self._ws_task_done, ws))
        return True

    def _scan_ws_headers(self) -> tuple[bool, bytes | None, bytes | None, bytes, bytes | None]:
        """Read the handshake fields off the parsed headers (RFC 6455 Sec. 4.2.1).

        `is_ws_upgrade` is True only when both halves of the upgrade triplet are
        present. `connection` may be a comma list ("keep-alive, Upgrade").
        """
        has_upgrade_token = False
        upgrade_is_ws = False
        ws_key: bytes | None = None
        ws_version: bytes | None = None
        host = b""
        origin: bytes | None = None
        for name, value in self.headers:
            if name == _WS_HEADER_CONNECTION:
                for token in value.split(b","):
                    if token.strip().lower() == _WS_HEADER_UPGRADE:
                        has_upgrade_token = True
                        break
            elif name == _WS_HEADER_UPGRADE:
                if value.strip().lower() == b"websocket":
                    upgrade_is_ws = True
            elif name == _WS_HEADER_KEY:
                if value:
                    ws_key = value
            elif name == _WS_HEADER_VERSION:
                ws_version = value.strip()
            elif name == _WS_HEADER_HOST and not host:
                host = value
            elif name == _WS_HEADER_ORIGIN and origin is None:
                origin = value
        return has_upgrade_token and upgrade_is_ws, ws_key, ws_version, host, origin

    def _write_ws_http_error(self, status_code: int, reason: bytes, extra: bytes = b"") -> None:
        """Refuse a handshake with a plain HTTP response, then close.

        No pre-101 path has completed the upgrade, so the refusal is an ordinary
        HTTP/1.1 response (RFC 6455 Sec. 4.2.2 / Sec. 4.4), never a close frame.
        `extra` carries extra header lines, such as the 426's version advert.
        """
        self._oversized = True
        self._emit_http_error(status_code, reason, extra=extra)
