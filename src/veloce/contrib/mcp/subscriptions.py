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

from veloce.contrib.mcp._helpers import _DEFERRED_RESPONSE
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


# The `_meta` key every message on a listen stream carries, identifying which
# `subscriptions/listen` request it belongs to. On stdio all streams share one
# channel, so this is how a client demultiplexes them.
META_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"

# The notification filter a `subscriptions/listen` may ask for. The boolean topics
# map to the notification each one enables; `resourceSubscriptions` is a URI list
# and is handled separately.
LISTEN_TOPICS = {
    "toolsListChanged": "notifications/tools/list_changed",
    "promptsListChanged": "notifications/prompts/list_changed",
    "resourcesListChanged": "notifications/resources/list_changed",
}
RESOURCE_SUBSCRIPTIONS = "resourceSubscriptions"


def _stamp(message: dict[str, Any], subscription_id: Any) -> dict[str, Any]:
    """Return `message` carrying its subscription id, without mutating the original.

    Every message on a stream carries the id, so one built notification can be
    stamped per subscriber rather than rebuilt.
    """
    params = dict(message.get("params") or {})
    meta = dict(params.get("_meta") or {})
    meta[META_SUBSCRIPTION_ID] = subscription_id
    params["_meta"] = meta
    return {**message, "params": params}


def subscription_acknowledged_notification(
    subscription_id: Any, agreed: dict[str, Any]
) -> dict[str, Any]:
    """Build the acknowledgement that must precede any notification on a stream.

    `agreed` reflects the subset of the requested filter the server will honour;
    unsupported types are omitted rather than echoed back.
    """
    return {
        "jsonrpc": "2.0",
        "method": "notifications/subscriptions/acknowledged",
        "params": {"_meta": {META_SUBSCRIPTION_ID: subscription_id}, "notifications": agreed},
    }


def subscription_closed_response(subscription_id: Any) -> dict[str, Any]:
    """Build the response that ends a stream gracefully.

    The JSON-RPC response to the long-lived `subscriptions/listen` request, sent
    when the server ends the subscription itself. A stream that drops without it
    signals an abrupt disconnect the client may reconnect from.
    """
    return {
        "jsonrpc": "2.0",
        "id": subscription_id,
        "result": {
            "resultType": "complete",
            "_meta": {META_SUBSCRIPTION_ID: subscription_id},
        },
    }


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
        """Send `notifications/resources/updated` to whoever asked for this URI.

        Two mechanisms coexist: the handshake-era `resources/subscribe` set, and a
        modern `subscriptions/listen` naming the URI in `resourceSubscriptions`.
        A listen stream's copy carries its subscription id.
        """
        message = resource_updated_notification(uri)
        for session, sink in list(self._sinks.values()):
            if uri in session.subscriptions:
                await self._send(sink, message)
            for subscription_id, wanted in session.listen_streams.items():
                if uri in wanted.get(RESOURCE_SUBSCRIPTIONS, ()):
                    await self._send(sink, _stamp(message, subscription_id))

    async def notify_list_changed(self) -> None:
        """Send `notifications/resources/list_changed` where it was asked for."""
        await self.notify_topic("resourcesListChanged", broadcast_unlistened=True)

    async def notify_topic(self, topic: str, *, broadcast_unlistened: bool = False) -> None:
        """Send one list-changed notification to the streams that asked for it.

        The spec forbids sending a notification type the client did not request, so
        a stream receives this only if its filter names `topic`. `broadcast_unlistened`
        preserves the pre-existing behaviour of the handshake-era resource
        notification, which goes to every open connection.
        """
        method = LISTEN_TOPICS.get(topic)
        if method is None:  # pragma: no cover - guarded by the caller
            return
        message = {"jsonrpc": "2.0", "method": method}
        for session, sink in list(self._sinks.values()):
            if broadcast_unlistened and not session.listen_streams:
                await self._send(sink, message)
            for subscription_id, wanted in session.listen_streams.items():
                if wanted.get(topic):
                    await self._send(sink, _stamp(message, subscription_id))

    def forget_streams(self, token: object) -> None:
        """Drop the listen streams a closing connection held, sending nothing.

        The transport is already gone, so a graceful close cannot be delivered;
        this only stops the fan-out from walking a session no longer reachable.
        """
        entry = self._sinks.get(token)
        if entry is not None:
            entry[0].listen_streams.clear()

    async def close_streams(self, session: MCPSession) -> None:
        """End a connection's streams gracefully, then forget them.

        Sent when the server tears a connection down on its own initiative, so the
        client can tell a clean close from a dropped transport.
        """
        if not session.listen_streams:
            return
        sinks = [sink for held, sink in self._sinks.values() if held is session]
        for subscription_id in list(session.listen_streams):
            for sink in sinks:
                await self._send(sink, subscription_closed_response(subscription_id))
        session.listen_streams.clear()

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

    def advertise(self, *, modern: bool = False) -> dict[str, Any] | None:
        return None

    def handlers(self) -> dict[str, MethodHandler]:
        return {
            "resources/subscribe": self._subscribe,
            "resources/unsubscribe": self._unsubscribe,
            "subscriptions/listen": self._listen,
        }

    async def _listen(self, params: dict[str, Any]) -> Any:
        """Open a long-lived notification stream (`subscriptions/listen`).

        The stream is identified by the JSON-RPC id of this request, which every
        message on it carries. The acknowledgement is sent first and reports the
        subset of the requested filter the server will honour; a type this server
        cannot serve is omitted rather than echoed, and is never sent.

        No response is produced now: this request is answered only when the stream
        ends, so the caller defers it.
        """
        session = self._require_session("subscriptions/listen")
        subscription_id = self._server.current_request_id()
        if subscription_id is None:
            raise InvalidParamsError(
                "subscriptions/listen must be a request with an id; the id "
                "identifies the stream it opens."
            )
        agreed = _agreed_filter(params.get("notifications"))
        session.listen_streams[subscription_id] = agreed
        await self._server.send_to_current_connection(
            subscription_acknowledged_notification(subscription_id, agreed)
        )
        return _DEFERRED_RESPONSE

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

    def _require_session(self, method: str = "resources/subscribe") -> MCPSession:
        """Return the dispatching connection's session, or reject a sessionless call.

        Subscriptions are per-connection state, so a subscribe / unsubscribe is
        meaningful only on a stateful transport that threads its session into the
        dispatch. A stateless call (no session) cannot hold a subscription, which
        is an invalid request for this method.
        """
        session = self._server.current_session()
        if session is None or not session.persistent:
            raise InvalidParamsError(
                f"{method} requires a stateful connection; subscriptions are not "
                "supported on a stateless request."
            )
        return session


def _agreed_filter(requested: Any) -> dict[str, Any]:
    """Return the subset of a requested notification filter this server will honour.

    Every field is optional and an unknown one is dropped: the acknowledgement
    reports only what will actually be sent, so a client can compare it against
    what it asked for.
    """
    if not isinstance(requested, dict):
        return {}
    agreed: dict[str, Any] = {}
    for topic in LISTEN_TOPICS:
        if requested.get(topic) is True:
            agreed[topic] = True
    uris = requested.get(RESOURCE_SUBSCRIPTIONS)
    if isinstance(uris, list):
        wanted = [uri for uri in uris if isinstance(uri, str) and uri]
        if wanted:
            agreed[RESOURCE_SUBSCRIPTIONS] = wanted
    return agreed


def _subscription_uri(params: dict[str, Any]) -> str:
    """Return the `uri` a subscribe / unsubscribe names, or reject a malformed one."""
    uri = params.get("uri")
    if not isinstance(uri, str) or not uri:
        raise InvalidParamsError("resources/subscribe requires a string 'uri'")
    return uri
