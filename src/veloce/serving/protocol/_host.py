"""The `HttpProtocol` members its mixins read and write.

Declared for the type checker only. At runtime `ProtocolHost` is `object`: the
state lives in `HttpProtocol.__slots__` and the methods on `HttpProtocol` and
its sibling mixin, so this adds nothing to the MRO's cost.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce
    from veloce.http._body import RequestBodySource
    from veloce.websocket import WebSocket

    class ProtocolHost:
        """Attributes and cross-calls the serving mixins rely on."""

        app: Veloce
        loop: asyncio.AbstractEventLoop
        transport: asyncio.Transport | None
        parser: Any
        url: bytes
        headers: list[tuple[bytes, bytes]]
        _active_tasks: set[asyncio.Task[Any]]
        _raw_content_length: bytes | None
        _has_expect_continue: bool
        _can_write: asyncio.Event
        _websocket: WebSocket | None
        _ws_task: asyncio.Task[Any] | None
        _keep_alive_handle: asyncio.TimerHandle | None
        _request_timer: asyncio.TimerHandle | None
        _current_source: RequestBodySource | None

        def _reset_header_state(self) -> None: ...
        async def drain(self) -> None: ...
        def _emit_http_error(
            self, status_code: int, reason: bytes, body: bytes = b"", extra: bytes = b""
        ) -> None: ...
        def _parse_request_target(self) -> tuple[str, bytes]: ...
        @staticmethod
        def _ws_task_done(ws: WebSocket, task: asyncio.Task[Any]) -> None: ...

else:
    ProtocolHost = object
