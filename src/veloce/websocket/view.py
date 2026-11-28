"""The handshake as the handler sees it — URL, query, cookies, origin, subprotocols."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veloce._constants import (
    HEADER_COOKIE,
    HEADER_ORIGIN,
    HEADER_SEC_WEBSOCKET_PROTOCOL,
)
from veloce.http.cookies import parse_cookie
from veloce.http.datastructures import Address, QueryParams, State
from veloce.websocket.state import WebSocketState

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable


if TYPE_CHECKING:  # pragma: no cover

    class _Host:
        """The `WebSocket` state this mixin reads, declared for the type checker only."""

        headers: dict[str, str]
        scope: dict[str, Any] | None
        path: str
        _accepted: bool
        _closed: bool
        _cookies: dict[str, str] | None
        _query_params: Any
        _state: Any

else:
    _Host = object


class HandshakeViewMixin(_Host):
    """Read-only views of the handshake: URL, query, cookies, origin, subprotocols."""

    __slots__ = ()

    def _peek_header(self, name: str) -> str | None:
        """Read one handshake header, as `Request` does.

        The dependency resolver passes a `WebSocket` wherever an HTTP resolve passes
        a `Request`, so anything reading a header off "the connection" - a `Header()`
        parameter, `HTTPBearer`, `HTTPBasic` - lands here and must find both methods.
        """
        return self.headers.get(name.lower())

    def _peek_header_key(self, key: bytes) -> str | None:
        """`_peek_header` for a caller holding the lowercase key as bytes."""
        return self.headers.get(key.decode("latin-1"))

    @property
    def query_params(self) -> Any:
        """Parsed query string of the WebSocket handshake URL.

        Read it as `ws.query_params["token"]`. Backed by
        `QueryParams` (multi-value, `getlist`-aware). Empty when the
        scope carries no `query_string`.
        """
        if self._query_params is not None:
            return self._query_params
        raw = b""
        if self.scope:
            raw = self.scope.get("query_string", b"") or b""
        qs = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
        self._query_params = QueryParams.from_query_string(qs)
        return self._query_params

    @property
    def url(self) -> str:
        """The WebSocket handshake URL path - ASGI-style shape.

        Returns `path` plus `?query` when a query string is present.
        """
        raw = b""
        if self.scope:
            raw = self.scope.get("query_string", b"") or b""
        qs = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
        return f"{self.path}?{qs}" if qs else self.path

    @property
    def client(self) -> Any:
        """The connecting peer as an `Address(host, port)`.

        Reads `scope["client"]` (the ASGI `(host, port)` pair).
        Returns `None` when the scope carries no client info.
        """
        client = self.scope.get("client") if self.scope else None
        if client:
            return Address(client[0], client[1])
        return None

    @property
    def state(self) -> Any:
        """Per-connection scratch namespace.

        Lazily-created `State` (a dict subclass) supporting both
        `ws.state.user = ...` attribute access and `ws.state["user"]`.
        """
        existing = self._state
        if existing is None:
            existing = self._state = State()
        return existing

    @property
    def cookies(self) -> dict[str, str]:
        """Cookies sent with the WebSocket handshake.

        Parses the handshake `Cookie` header into `{name: value}`.
        Empty when no cookie header was present.
        """
        if self._cookies is not None:
            return self._cookies
        self._cookies = parse_cookie(self.headers.get(HEADER_COOKIE.lower(), ""))
        return self._cookies

    @property
    def application_state(self) -> WebSocketState:
        """Server-side state of the WebSocket.

        - `CONNECTING` before the app sends `accept()`.
        - `CONNECTED` after `accept()` and until `close()` is sent.
        - `DISCONNECTED` after `close()` (locally) or after the peer
          half-closes (observed via `WebSocketDisconnect`).
        """
        if self._closed:
            return WebSocketState.DISCONNECTED
        if self._accepted:
            return WebSocketState.CONNECTED
        return WebSocketState.CONNECTING

    @property
    def client_state(self) -> WebSocketState:
        """Client-side state of the WebSocket.

        Veloce does not distinguish the two halves at the protocol level
        beyond the close flag, so this mirrors `application_state` once
        the peer disconnects and otherwise stays `CONNECTED` once the
        handshake completes.
        """
        return self.application_state

    @property
    def origin(self) -> str | None:
        """The client-supplied `Origin` header, or `None` if absent.

        WebSocket handshakes carry `Origin` per RFC 6455 Sec. 10.2 / Sec. 4.1.
        Browsers always send it; non-browser clients may omit it. The
        header is the application's primary defence against Cross-Site
        WebSocket Hijacking - CSWSH bypasses CORS because the handshake
        is plain HTTP/1.1 and Same-Origin Policy does not apply to it.
        Pair this accessor with `check_origin(allowed)` before `accept()`.
        """
        return self.headers.get(HEADER_ORIGIN.lower())

    def check_origin(self, allowed: str | Iterable[str]) -> bool:
        """Return `True` when the handshake's `Origin` is in `allowed`.

        Pass a single origin string or an iterable of them. Normalisation
        matches `WebSocketOriginMiddleware` - each side lowercased and stripped
        of a trailing slash - so an allow-list written for one works with the
        other.

        - **Wildcard.** `"*"` in `allowed` accepts any origin, the opt-in "I
          have my own check elsewhere" escape hatch.
        - **Missing `Origin`** (absent, or a literal `Origin: null` from a
          sandboxed iframe or `file://` page) returns `False`. Non-browser
          clients legitimately omit it; branch on `ws.origin is None` to allow
          them, or use `WebSocketOriginMiddleware`'s `allow_missing=True`.

        Usage::

            @app.websocket("/ws")
            async def chat(ws: WebSocket):
                if not ws.check_origin("https://app.example.com"):
                    await ws.close(code=WS_1008_POLICY_VIOLATION)  # policy violation
                    return
                await ws.accept()
                ...

        For a check registered once, before the handler, reach for
        `WebSocketOriginMiddleware`.
        """
        allowed_set = frozenset((allowed,)) if isinstance(allowed, str) else frozenset(allowed)
        if "*" in allowed_set:
            return True
        if self.origin is None or self.origin == "null":
            return False
        origin_norm = self.origin.rstrip("/").lower()
        normalised = {a.rstrip("/").lower() for a in allowed_set}
        return origin_norm in normalised

    @property
    def requested_subprotocols(self) -> list[str]:
        """Subprotocols the client offered in `Sec-WebSocket-Protocol`.

        Returns them in client preference order (RFC 6455 Sec. 1.9). Empty
        list when the header is absent. Whitespace around each token is
        stripped; the comparison the negotiator performs is case-sensitive
        per the spec.
        """
        raw = self.headers.get(HEADER_SEC_WEBSOCKET_PROTOCOL.lower(), "")
        if not raw:
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]

    def negotiate_subprotocol(self, supported: list[str]) -> str | None:
        """Pick the first client-offered subprotocol that the server supports.

        Per RFC 6455 Sec. 4.1, the server picks ONE protocol from the client's
        list. Most servers prefer to honour the client's preference order
        (first match wins), which is what we do.
        """
        offered = self.requested_subprotocols
        if not offered:
            return None
        wanted = set(supported)
        for proto in offered:
            if proto in wanted:
                return proto
        return None
