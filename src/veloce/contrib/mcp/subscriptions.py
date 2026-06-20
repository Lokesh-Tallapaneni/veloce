"""MCP resource subscriptions — per-connection resource-change notifications.

The Model Context Protocol lets a client subscribe to a resource URI and receive
a `notifications/resources/updated` whenever that resource changes, plus a
`notifications/resources/list_changed` when the set of available resources
changes. Subscriptions are per-connection: a connection's subscribed URIs live on
its `MCPSession`, and a notification reaches only the connections that subscribed.

A change is signalled by the application, not derived by the framework — a route's
data layer knows when its resource changed, so the app calls
`MCPServer.notify_resource_updated(uri)` (or `notify_resources_list_changed()`)
and the server fans the notification out to subscribed connections through their
outbound sinks. This `ConnectionRegistry` holds the live connections so a fan-out
is a set walk, not a scan of every possible client.

The feature is opt-in (`MCP_RESOURCE_SUBSCRIPTIONS` in `app.config`): a server
with it off advertises `subscribe`/`listChanged` as `false`, registers no
subscribe / unsubscribe handlers, and tracks no connections, so the default path
allocates nothing and pays no per-request cost.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from veloce.contrib.mcp.capabilities.base import Capability
from veloce.contrib.mcp.errors import InvalidParamsError

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.server import MCPServer, MethodHandler
    from veloce.contrib.mcp.session import MCPSession

_logger = logging.getLogger(__name__)

# An outbound one-way sink: the connection's `Transport.send`, which delivers a
# server-initiated JSON-RPC notification to that one client.
Sink = Callable[[dict[str, Any]], Awaitable[None]]


def resource_updated_notification(uri: str) -> dict[str, Any]:
    """Build the `notifications/resources/updated` message for a changed resource."""
    return {
        "jsonrpc": "2.0",
        "method": "notifications/resources/updated",
        "params": {"uri": uri},
    }


def resources_list_changed_notification() -> dict[str, Any]:
    """Build the `notifications/resources/list_changed` message."""
    return {"jsonrpc": "2.0", "method": "notifications/resources/list_changed"}


class ConnectionRegistry:
    """The live MCP connections, each pairing its session with its outbound sink.

    A stateful transport registers its connection for the duration it is open so
    an application-signalled resource change can be fanned out to the connections
    subscribed to that resource. A connection is keyed by its `MCPSession`
    identity, so registering twice is idempotent and removal is exact.
    """

    __slots__ = ("_sinks",)

    def __init__(self) -> None:
        self._sinks: dict[MCPSession, Sink] = {}

    def add(self, session: MCPSession, sink: Sink) -> None:
        """Record an open connection's session and its outbound sink."""
        self._sinks[session] = sink

    def remove(self, session: MCPSession) -> None:
        """Drop a closed connection so it no longer receives notifications."""
        self._sinks.pop(session, None)

    async def notify_updated(self, uri: str) -> None:
        """Send `notifications/resources/updated` to connections subscribed to `uri`."""
        message = resource_updated_notification(uri)
        for session, sink in list(self._sinks.items()):
            if uri in session.subscriptions:
                await self._send(sink, message)

    async def notify_list_changed(self) -> None:
        """Send `notifications/resources/list_changed` to every open connection."""
        message = resources_list_changed_notification()
        for sink in list(self._sinks.values()):
            await self._send(sink, message)

    @staticmethod
    async def _send(sink: Sink, message: dict[str, Any]) -> None:
        # A dead or slow sink must not fail the caller's signal: a notification is
        # advisory, and one closed connection cannot block the others.
        try:
            await sink(message)
        except Exception:  # pragma: no cover - defensive against a torn-down sink
            _logger.exception("MCP resource notification delivery failed")


class SubscriptionsCapability(Capability):
    """The `resources/subscribe` / `resources/unsubscribe` methods, opt-in.

    Folded into the resource area but kept a separate capability so the base
    `ResourcesCapability` stays unchanged when subscriptions are off. It
    contributes no `initialize` entry of its own — the resource advertisement
    (the `subscribe`/`listChanged` sub-capability flags) lives on
    `ResourcesCapability`, which reads the same opt-in flag — so `advertise`
    returns `None`.
    """

    __slots__ = ("_server",)

    def __init__(self, server: MCPServer) -> None:
        self._server = server

    def advertise(self) -> dict[str, Any] | None:
        return None

    def handlers(self) -> dict[str, MethodHandler]:
        return {
            "resources/subscribe": self._subscribe,
            "resources/unsubscribe": self._unsubscribe,
        }

    async def _subscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        """Record this connection's interest in a resource URI (`resources/subscribe`)."""
        session = self._require_session()
        session.subscriptions.add(_subscription_uri(params))
        return {}

    async def _unsubscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        """Drop this connection's interest in a resource URI (`resources/unsubscribe`)."""
        session = self._require_session()
        session.subscriptions.discard(_subscription_uri(params))
        return {}

    def _require_session(self) -> MCPSession:
        """Return the dispatching connection's session, or reject a sessionless call.

        Subscriptions are per-connection state, so a subscribe / unsubscribe is
        meaningful only on a stateful transport that threads its session into the
        dispatch. A stateless call (no session) cannot hold a subscription, which
        is an invalid request for this method.
        """
        session = self._server.current_session()
        if session is None:
            raise InvalidParamsError(
                "resources/subscribe requires a stateful connection; subscriptions "
                "are not supported on a stateless request."
            )
        return session


def _subscription_uri(params: dict[str, Any]) -> str:
    """Return the `uri` a subscribe / unsubscribe names, or reject a malformed one."""
    uri = params.get("uri")
    if not isinstance(uri, str) or not uri:
        raise InvalidParamsError("resources/subscribe requires a string 'uri'")
    return uri
