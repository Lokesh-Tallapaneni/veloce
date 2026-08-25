"""Concrete MCP capabilities — tools, resources, prompts, logging.

Each capability wraps the server handlers for its spec area and contributes
both its dispatch-map entries (`handlers`) and its `initialize` advertisement
(`advertise`). Resources and prompts advertise only when the app exposes at
least one, so the client does not probe an empty primitive. Tools are always
advertised, because every server serves tools. Logging is advertised in the
`initialize` handshake for the same reason - any tool may emit a log message the
client gates with ``logging/setLevel`` - but not on the modern discovery
document, where the revision dropped it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veloce.contrib.mcp.capabilities.base import _ServerCapability

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.server import MethodHandler


class ToolsCapability(_ServerCapability):
    """The ``tools/list`` and ``tools/call`` methods, always advertised."""

    __slots__ = ()

    def advertise(self, *, modern: bool = False) -> dict[str, Any]:
        # The registry is fixed once the server is built, but what a connection is
        # *listed* is not: a handler may narrow this connection's view with
        # `MCPContext.hide`, and the client is told so it fetches the list again.
        return {"tools": {"listChanged": self._connection_can_be_told()}}

    def handlers(self) -> dict[str, MethodHandler]:
        return {
            "tools/list": self._server._handle_tools_list,
            "tools/call": self._server._tools_call,
        }


class ResourcesCapability(_ServerCapability):
    """The resource methods, advertised only when the app exposes a resource."""

    __slots__ = ()

    def advertise(self, *, modern: bool = False) -> dict[str, Any] | None:
        # The `subscribe`/`listChanged` sub-capabilities are advertised only when
        # the app opts into resource subscriptions AND the connection answering
        # `initialize` is stateful. Subscriptions are per-connection state delivered
        # over the connection's outbound stream, so a stateless request (no session)
        # cannot serve them; advertising `true` there would invite a client to probe
        # a primitive that errors. A stateful connection (the stdio loop, or an HTTP
        # `Mcp-Session-Id` session) advertises and serves them.
        if not self._server.resources.resources:
            return None
        stateful = self._connection_can_be_told()
        # `subscribe` additionally needs the subscription machinery; `listChanged`
        # needs only the channel, because `MCPContext.hide` can narrow a
        # connection's resource listing whether or not subscriptions are on.
        return {
            "resources": {
                "subscribe": self._server._subscriptions_enabled and stateful,
                "listChanged": stateful,
            }
        }

    def handlers(self) -> dict[str, MethodHandler]:
        return {
            "resources/list": self._server._handle_resources_list,
            "resources/templates/list": self._server._handle_resource_templates_list,
            "resources/read": self._server._resources_read,
        }


class PromptsCapability(_ServerCapability):
    """The prompt methods, advertised only when the app exposes a prompt."""

    __slots__ = ()

    def advertise(self, *, modern: bool = False) -> dict[str, Any] | None:
        if not self._server.prompts.prompts:
            return None
        return {"prompts": {"listChanged": self._connection_can_be_told()}}

    def handlers(self) -> dict[str, MethodHandler]:
        return {
            "prompts/list": self._server._handle_prompts_list,
            "prompts/get": self._server._prompts_get,
        }


class LoggingCapability(_ServerCapability):
    """The ``logging/setLevel`` method, advertised on the revisions that have it.

    Any tool may emit a log message through `MCPContext.log`, and a handshake-era
    client may raise the minimum level once per connection. The modern revision
    removed the method - a client sets its level per request in `_meta` - so the
    capability is withheld there rather than advertised and then refused.
    """

    __slots__ = ()

    handshake_only_methods = frozenset({"logging/setLevel"})

    def advertise(self, *, modern: bool = False) -> dict[str, Any] | None:
        return None if modern else {"logging": {}}

    def handlers(self) -> dict[str, MethodHandler]:
        return {"logging/setLevel": self._server._handle_set_log_level}
