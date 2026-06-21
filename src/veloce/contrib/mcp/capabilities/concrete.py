"""Concrete MCP capabilities — tools, resources, prompts, logging.

Each capability wraps the server handlers for its spec area and contributes
both its dispatch-map entries (`handlers`) and its `initialize` advertisement
(`advertise`). Resources and prompts advertise only when the app exposes at
least one, so the client does not probe an empty primitive. Tools and logging
are always advertised: every server serves tools, and any tool may emit a log
message the client can gate with ``logging/setLevel``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veloce.contrib.mcp.capabilities.base import Capability

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.server import MCPServer, MethodHandler


class ToolsCapability(Capability):
    """The ``tools/list`` and ``tools/call`` methods, always advertised."""

    __slots__ = ("_server",)

    def __init__(self, server: MCPServer) -> None:
        self._server = server

    def advertise(self) -> dict[str, Any]:
        # `listChanged` is off: the tool set is fixed once the server is built.
        return {"tools": {"listChanged": False}}

    def handlers(self) -> dict[str, MethodHandler]:
        return {
            "tools/list": self._server._handle_tools_list,
            "tools/call": self._server._tools_call,
        }


class ResourcesCapability(Capability):
    """The resource methods, advertised only when the app exposes a resource."""

    __slots__ = ("_server",)

    def __init__(self, server: MCPServer) -> None:
        self._server = server

    def advertise(self) -> dict[str, Any] | None:
        # `subscribe`/`listChanged` are off: resources are served on demand,
        # with no update notifications on the serial loop.
        if not self._server.resources.resources:
            return None
        return {"resources": {"subscribe": False, "listChanged": False}}

    def handlers(self) -> dict[str, MethodHandler]:
        return {
            "resources/list": self._server._handle_resources_list,
            "resources/templates/list": self._server._handle_resource_templates_list,
            "resources/read": self._server._resources_read,
        }


class PromptsCapability(Capability):
    """The prompt methods, advertised only when the app exposes a prompt."""

    __slots__ = ("_server",)

    def __init__(self, server: MCPServer) -> None:
        self._server = server

    def advertise(self) -> dict[str, Any] | None:
        if not self._server.prompts.prompts:
            return None
        return {"prompts": {"listChanged": False}}

    def handlers(self) -> dict[str, MethodHandler]:
        return {
            "prompts/list": self._server._handle_prompts_list,
            "prompts/get": self._server._prompts_get,
        }


class LoggingCapability(Capability):
    """The ``logging/setLevel`` method, always advertised.

    Any tool may emit a log message through `MCPContext.log`, and the client may
    raise the minimum level, so logging is advertised for every server.
    """

    __slots__ = ("_server",)

    def __init__(self, server: MCPServer) -> None:
        self._server = server

    def advertise(self) -> dict[str, Any]:
        return {"logging": {}}

    def handlers(self) -> dict[str, MethodHandler]:
        return {"logging/setLevel": self._server._handle_set_log_level}
