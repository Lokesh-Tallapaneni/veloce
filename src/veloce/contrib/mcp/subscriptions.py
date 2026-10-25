"""MCP resource subscriptions — per-connection resource-change notifications.

A client subscribes to a resource URI and receives `notifications/resources/
updated` when it changes, plus `notifications/resources/list_changed` when the
resource set does. Subscriptions are per-connection: the subscribed URIs live on
the `MCPSession`, so a notification reaches only the connections that subscribed.

A change is signalled by the application, not derived by the framework: the app
calls `MCPServer.notify_resource_updated(uri)` and the server fans out through
each connection's outbound sink. `ConnectionRegistry` holds the live connections
so that fan-out is a set walk, not a scan of every possible client.

Opt-in via `MCP_RESOURCE_SUBSCRIPTIONS`: with it off nothing is advertised,
registered or tracked, so the default path pays no per-request cost.
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

# Names the `subscriptions/listen` request a message belongs to. On stdio all
# streams share one channel, so this is how a client demultiplexes them.
META_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"

# The boolean topics a `subscriptions/listen` filter may ask for;
# `resourceSubscriptions` is a URI list and is handled separately.
LISTEN_TOPICS = {
    "toolsListChanged": "notifications/tools/list_changed",
    "promptsListChanged": "notifications/prompts/list_changed",
    "resourcesListChanged": "notifications/resources/list_changed",
}
RESOURCE_SUBSCRIPTIONS = "resourceSubscriptions"

# The connection's `Transport.send`: one-way delivery to that one client.
Sink = Callable[[dict[str, Any]], Awaitable[None]]


# ── Notification shapes ───────────────────────────────────


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


def _stamp(message: dict[str, Any], subscription_id: Any) -> dict[str, Any]:
    """Return `message` carrying its subscription id, without mutating the original.

    One built notification is stamped per subscriber rather than rebuilt.
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

    The response to the long-lived `subscriptions/listen` request. A stream that
    drops without it signals an abrupt disconnect the client may reconnect from.
    """
    return {
        "jsonrpc": "2.0",
        "id": subscription_id,
        "result": {
            "resultType": "complete",
            "_meta": {META_SUBSCRIPTION_ID: subscription_id},
        },
    }


# ── The connection registry ───────────────────────────────


class ConnectionRegistry:
    """The live MCP connections, each pairing its session with its outbound sink.

    Each registration is keyed by an opaque token rather than by session, so two
    concurrent streams on one session id are tracked independently and neither
    silences the other on unregister. A `session -> tokens` index keeps the
    fan-out a set walk and drops an evicted session's streams at once.
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

    def holds(self, session: MCPSession) -> bool:
        """Whether this session has an open outbound stream to deliver on."""
        return session in self._by_session

    async def notify_updated(self, uri: str) -> None:
        """Send `notifications/resources/updated` to whoever asked for this URI.

        Two mechanisms coexist: the handshake-era `resources/subscribe` set, and a
        modern `subscriptions/listen` naming the URI in `resourceSubscriptions`.
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

        The spec forbids sending a notification type the client did not request,
        so a stream receives this only if its filter names `topic`.
        `broadcast_unlistened` is the handshake-era resource notification, which
        goes to every open connection.
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

        The transport is already gone, so no graceful close can be delivered.
        """
        entry = self._sinks.get(token)
        if entry is not None:
            entry[0].listen_streams.clear()

    async def close_streams(self, session: MCPSession) -> None:
        """End a connection's streams gracefully, then forget them.

        Lets the client tell a clean close from a dropped transport.
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
        # A notification is advisory, so one dead sink cannot fail the signal.
        try:
            await sink(message)
        except Exception:  # pragma: no cover - defensive against a torn-down sink
            _logger.exception("MCP resource notification delivery failed")


# ── The capability ────────────────────────────────────────


class SubscriptionsCapability(_ServerCapability):
    """The `resources/subscribe` / `resources/unsubscribe` methods, opt-in.

    Separate from `ResourcesCapability` so that stays unchanged when
    subscriptions are off. `advertise` returns `None` because the
    `subscribe`/`listChanged` flags live on `ResourcesCapability`, which reads
    the same opt-in flag.
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

        The stream is identified by this request's JSON-RPC id, which every
        message on it carries. The acknowledgement goes first and reports the
        subset of the filter the server will honour. No response is produced
        now: the request is answered when the stream ends, so the caller defers it.
        """
        session = self._require_open_stream()
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
        session = self._require_session("resources/subscribe")
        session.subscriptions.add(_subscription_uri(params))
        return {}

    async def _unsubscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        """Drop this connection's interest in a resource URI (`resources/unsubscribe`)."""
        session = self._require_session("resources/unsubscribe")
        session.subscriptions.discard(_subscription_uri(params))
        return {}

    def _require_open_stream(self) -> MCPSession:
        """Return the session that will hold this `subscriptions/listen` stream.

        A listen stream needs somewhere to deliver, not a session that outlives
        the request: the open stream *is* the subscription and ends with the
        request, so a persistent connection is deliberately not required.
        """
        session = self._server.current_session()
        if session is None or not self._server.connection_can_stream(session):
            raise InvalidParamsError(
                "subscriptions/listen requires an open stream to deliver on; "
                "send it on a connection the server can push messages down."
            )
        return session

    def _require_session(self, method: str) -> MCPSession:
        """Return the dispatching connection's session, or reject a sessionless call.

        Subscriptions are per-connection state, so a stateless call cannot hold
        one and is an invalid request for this method.
        """
        session = self._server.current_session()
        if session is None or not session.persistent:
            raise InvalidParamsError(
                f"{method} requires a stateful connection; subscriptions are not "
                "supported on a stateless request."
            )
        return session


# ── Parameter parsing ─────────────────────────────────────


def _agreed_filter(requested: Any) -> dict[str, Any]:
    """Return the subset of a requested notification filter this server will honour.

    Every field is optional and an unknown one is dropped, so the
    acknowledgement reports only what will actually be sent.
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
