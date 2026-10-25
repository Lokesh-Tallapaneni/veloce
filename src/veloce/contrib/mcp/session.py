"""MCPSession — per-connection lifecycle state for a stateful transport.

Per the MCP lifecycle the initialization exchange MUST be the first interaction:
until it completes a server answers only `initialize` and `ping`, and this state
lets a stateful transport enforce that ordering.
"""

from __future__ import annotations

import itertools
import secrets
from typing import Any

from veloce.contrib.mcp._helpers import META_CLIENT_CAPABILITIES, META_CLIENT_INFO

# Never reused, unlike `id(session)` (an address CPython recycles): a task that
# outlives its evicted session cannot be matched by a later session.
_connection_id_counter = itertools.count(1)

# The counter above restarts at 1 in every pre-forked worker, so a per-process
# prefix keeps the public identity from pooling unrelated clients in one bucket.
_PROCESS_TOKEN = secrets.token_hex(4)


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
        "listen_streams",
        "hidden",
        "persistent",
        "log_level",
    )

    def __init__(self, persistent: bool = True) -> None:
        # The ownership key for tasks and the in-flight registry.
        self.connection_id = next(_connection_id_counter)
        # Whether the session outlives a single message. A stateless HTTP POST gets
        # a throwaway one, which cannot subscribe or advertise connection features.
        self.persistent = persistent
        self.initialized = False
        # The server consults this before relying on a client feature.
        self.client_capabilities: dict[str, Any] = {}
        # Whatever `clientInfo` carried - name / version / title.
        self.client_info: dict[str, Any] | None = None
        # URIs from `resources/subscribe`; `notifications/resources/updated` goes
        # only to a connection holding the changed URI here.
        self.subscriptions: set[str] = set()
        # Open `subscriptions/listen` streams, keyed by the JSON-RPC id of the
        # opening request - which the spec defines as the subscription id.
        self.listen_streams: dict[Any, dict[str, Any]] = {}
        # Primitives this connection asked not to see, so a running call narrows
        # its own client's view without touching what anyone else is served.
        self.hidden: set[str] = set()
        # Minimum level for `notifications/message`, set by `logging/setLevel`. The
        # spec scopes it to the connection, so it lives here, not in a ContextVar.
        self.log_level: str | None = None

    @property
    def public_id(self) -> str:
        """A globally unique identity for this connection, safe to key state on.

        `connection_id` alone restarts at 1 per worker, so it keys this process's
        registries and nothing more. Composed on access, not stored.
        """
        return f"{_PROCESS_TOKEN}-{self.connection_id}"

    def record_initialize(self, params: dict[str, Any]) -> None:
        """Record the client's advertised capabilities and info from `initialize`."""
        capabilities = params.get("capabilities")
        self.client_capabilities = capabilities if isinstance(capabilities, dict) else {}
        client_info = params.get("clientInfo")
        self.client_info = client_info if isinstance(client_info, dict) else None

    def record_request_meta(self, meta: dict[str, Any] | None) -> None:
        """Record the client identity a modern request carries in its `_meta`.

        The modern revision has no `initialize`. Absent keys leave the previous
        values alone, since a session may be persistent across requests.
        """
        if not isinstance(meta, dict):
            return
        client_info = meta.get(META_CLIENT_INFO)
        if isinstance(client_info, dict):
            self.client_info = client_info
        capabilities = meta.get(META_CLIENT_CAPABILITIES)
        if isinstance(capabilities, dict):
            self.client_capabilities = capabilities

    def supports(self, capability: str) -> bool:
        """Return whether the client advertised the named top-level capability."""
        return capability in self.client_capabilities
