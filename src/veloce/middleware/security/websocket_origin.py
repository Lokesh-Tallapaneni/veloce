"""WebSocket origin middleware — checks `Origin` against cross-site hijacking (RFC 6455 Sec. 4.1, Sec. 4.2.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from veloce.middleware.base import Middleware

if TYPE_CHECKING:  # pragma: no cover
    pass


# ── WebSocket origin ──────────────────────────────────────
class WebSocketOriginMiddleware(Middleware):
    """Reject cross-site WebSocket handshakes (CSWSH).

    A WebSocket handshake is not subject to the Same-Origin Policy and
    bypasses CORS entirely, so a page on any origin can open a socket to
    your app unless the handshake `Origin` is checked. Register this
    with the origins your own front-end is served from; a handshake whose
    `Origin` is present but unlisted is refused with close code 1008.

    Browsers always send `Origin` on a WebSocket handshake (RFC 6455
    Sec. 4.1), so `allow_missing=True` (the default) still blocks every
    browser-driven CSWSH attempt while leaving non-browser clients
    (mobile apps, service-to-service) - which legitimately omit `Origin`
    - able to connect. Set `allow_missing=False` to additionally refuse
    handshakes that carry no `Origin` at all.

    Plain HTTP requests pass straight through - `Origin` enforcement for
    HTTP is `CORSMiddleware`'s job.
    """

    def __init__(
        self, allowed_origins: list[str], *, allow_missing: bool = True, name: str | None = None
    ) -> None:
        """Set the allowed origins.

        Args:
            allowed_origins: Origins that may open a WebSocket; `*` allows any.
            allow_missing: Admit handshakes that carry no `Origin` header.
            name: Name for `exclude_middleware`.

        """
        super().__init__(name=name)
        self._allowed: frozenset[str] = frozenset(o.rstrip("/").lower() for o in allowed_origins)
        self._allow_all = "*" in self._allowed
        self._allow_missing = allow_missing

    def is_websocket_origin_allowed(self, origin: str) -> bool:
        """Whether a handshake carrying `origin` may proceed.

        Public so the WebSocket dispatch path can apply the check - a
        handshake never reaches an HTTP middleware's `process_request`.
        """
        if self._allow_all:
            return True
        if not origin:
            return self._allow_missing
        return origin.rstrip("/").lower() in self._allowed
