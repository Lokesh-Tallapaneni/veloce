"""MCPSession — per-connection lifecycle state for a stateful transport.

A transport that keeps one connection alive across many messages (the serial
stdio loop) owns one `MCPSession` and threads it into every
`MCPServer.handle_message` call. The session records two things the spec ties to
the connection: whether `initialize` has run, and the capabilities the client
advertised in that `initialize` (so the server can later consult what the client
supports, e.g. before issuing a `sampling` / `elicitation` request).

Per the MCP lifecycle the initialization exchange MUST be the first interaction:
before it completes the only requests a server answers are `initialize` and
`ping`. The session lets the server enforce that ordering on a stateful
transport. The stateless HTTP transport passes no session - each POST is an
independent message with no connection to order against - so its fast path is
unaffected.
"""

from __future__ import annotations

import itertools
from typing import Any

# Monotonic source of per-session connection ids. A connection id is a stable
# identity for the session's lifetime and is never reused, unlike `id(session)`
# (a memory address CPython recycles once the session is freed). Task ownership
# and the in-flight registry key off this so a task that outlives its evicted
# session cannot be matched by a later session that lands on a recycled address.
_connection_id_counter = itertools.count(1)


# The `_meta` keys a modern request carries its client identity in. Duplicated as
# module constants rather than imported from `server`, which imports this module.
_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
_META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"


class MCPSession:
    """Lifecycle state for one stateful MCP connection.

    Holds whether the connection has initialized and the client's advertised
    capabilities / implementation info, recorded from the `initialize` request.
    """

    __slots__ = (
        "connection_id",
        "initialized",
        "client_capabilities",
        "client_info",
        "subscriptions",
        "persistent",
    )

    def __init__(self, persistent: bool = True) -> None:
        # A process-unique, never-recycled identity for this connection. Used as
        # the ownership key for tasks and the in-flight registry so ownership
        # cannot alias across a freed session's recycled `id()`.
        self.connection_id = next(_connection_id_counter)
        # Whether the session outlives a single message. The stdio loop and an HTTP
        # `Mcp-Session-Id` own a persistent session that carries connection state
        # (subscriptions, lifecycle) across messages; a stateless HTTP POST gets a
        # throwaway session only to isolate its in-flight registry, so it is not
        # persistent and cannot subscribe or advertise per-connection features.
        self.persistent = persistent
        self.initialized = False
        # The `capabilities` object the client sent in `initialize`; empty until
        # then. The server consults it before relying on a client feature.
        self.client_capabilities: dict[str, Any] = {}
        # The client's `clientInfo` (name / version / title), or `None` when the
        # client sent none.
        self.client_info: dict[str, Any] | None = None
        # Resource URIs this connection subscribed to via `resources/subscribe`;
        # the server emits `notifications/resources/updated` only to a connection
        # holding the changed URI here. Empty until the connection subscribes, so
        # a connection that never subscribes pays nothing.
        self.subscriptions: set[str] = set()

    def record_initialize(self, params: dict[str, Any]) -> None:
        """Record the client's advertised capabilities and info from `initialize`."""
        capabilities = params.get("capabilities")
        self.client_capabilities = capabilities if isinstance(capabilities, dict) else {}
        client_info = params.get("clientInfo")
        self.client_info = client_info if isinstance(client_info, dict) else None

    def record_request_meta(self, meta: dict[str, Any] | None) -> None:
        """Record the client identity a modern request carries in its `_meta`.

        The modern revision has no `initialize`: a client states who it is and what
        it supports on every request. Recording it here keeps one place -
        `client_info` / `client_capabilities` - answering for both eras, so nothing
        downstream has to know which handshake produced them. Absent keys leave the
        previous values alone rather than clearing them, since a session may be
        persistent across requests.
        """
        if not isinstance(meta, dict):
            return
        client_info = meta.get(_META_CLIENT_INFO)
        if isinstance(client_info, dict):
            self.client_info = client_info
        capabilities = meta.get(_META_CLIENT_CAPABILITIES)
        if isinstance(capabilities, dict):
            self.client_capabilities = capabilities

    def supports(self, capability: str) -> bool:
        """Return whether the client advertised the named top-level capability."""
        return capability in self.client_capabilities
