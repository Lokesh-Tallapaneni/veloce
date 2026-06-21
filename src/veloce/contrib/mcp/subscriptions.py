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

from veloce.contrib.mcp.capabilities.base import _ServerCapability
from veloce.contrib.mcp.errors import InvalidParamsError

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.server import MethodHandler
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
    subscribed to that resource. Each registration is keyed by an opaque token
    rather than by session, so two concurrent streams on one session id are
    tracked independently: each receives notifications and unregisters on its own
    without silencing the other. A `session -> tokens` index keeps the fan-out a
    set walk and lets an evicted session drop all of its streams at once.
    """

    __slots__ = ("_sinks", "_by_session")

    def __init__(self) -> None:
        self._sinks: dict[object, tuple[MCPSession, Sink]] = {}
        self._by_session: dict[MCPSession, set[object]] = {}

    def add(self, session: MCPSession, sink: Sink) -> object:
        """Record an open connection's session and sink; return its removal token."""
        token = object()
        self._sinks[token] = (session, sink)
        self._by_session.setdefault(session, set()).add(token)
        return token

    def remove(self, token: object) -> None:
        """Drop a single closed connection so it no longer receives notifications."""
        entry = self._sinks.pop(token, None)
        if entry is None:
            return
        session = entry[0]
        tokens = self._by_session.get(session)
        if tokens is not None:
            tokens.discard(token)
            if not tokens:
                del self._by_session[session]

    def remove_session(self, session: MCPSession) -> None:
        """Drop every connection a session holds (used when the session is evicted)."""
        for token in self._by_session.pop(session, ()):
            self._sinks.pop(token, None)

    async def notify_updated(self, uri: str) -> None:
        """Send `notifications/resources/updated` to connections subscribed to `uri`."""
        message = resource_updated_notification(uri)
        for session, sink in list(self._sinks.values()):
            if uri in session.subscriptions:
                await self._send(sink, message)

    async def notify_list_changed(self) -> None:
        """Send `notifications/resources/list_changed` to every open connection."""
        message = resources_list_changed_notification()
        for _session, sink in list(self._sinks.values()):
            await self._send(sink, message)

    @staticmethod
    async def _send(sink: Sink, message: dict[str, Any]) -> None:
        # A dead or slow sink must not fail the caller's signal: a notification is
        # advisory, and one closed connection cannot block the others.
        try:
            await sink(message)
        except Exception:  # pragma: no cover - defensive against a torn-down sink
            _logger.exception("MCP resource notification delivery failed")


class SubscriptionsCapability(_ServerCapability):
    """The `resources/subscribe` / `resources/unsubscribe` methods, opt-in.

    Folded into the resource area but kept a separate capability so the base
    `ResourcesCapability` stays unchanged when subscriptions are off. It
    contributes no `initialize` entry of its own — the resource advertisement
    (the `subscribe`/`listChanged` sub-capability flags) lives on
    `ResourcesCapability`, which reads the same opt-in flag — so `advertise`
    returns `None`.
    """

    __slots__ = ()

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
        if session is None or not session.persistent:
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
