"""Capability base — one MCP spec area's advertisement and method handlers.

The server holds a list of these: `initialize` merges every `advertise()`, and
the dispatch map merges every `handlers()`, so a new spec area is a new
capability rather than a new branch in a dispatcher.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veloce._internal import _require_slots

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.server import MCPServer, MethodHandler


class Capability:
    """One MCP spec area: its `initialize` advertisement and its method handlers."""

    __slots__ = ()

    #: Methods the modern revision retired, refused there with method-not-found.
    handshake_only_methods: frozenset[str] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # A subclass that forgets `__slots__` silently regains a `__dict__`.
        super().__init_subclass__(**kwargs)
        _require_slots(cls)

    def advertise(self, *, modern: bool = False) -> dict[str, Any] | None:
        """Return this capability's entry for the `initialize` capabilities object.

        A single-key mapping (``{"tools": {"listChanged": False}}``), or `None`
        when the app exposes nothing for this area. Declare a `modern` keyword
        parameter to vary the entry by protocol revision; the server passes it
        when the signature accepts it.
        """
        raise NotImplementedError

    def handlers(self) -> dict[str, MethodHandler]:
        """Return the ``{json_rpc_method: handler}`` map this capability answers."""
        raise NotImplementedError

    def extensions(self) -> dict[str, Any] | None:
        """Return this capability's entry for the advertised extensions object.

        `None` - the default - contributes no extension. Return an entry only
        when the feature it names is actually available, so a client is never
        offered one the server cannot serve.
        """
        return None


class _ServerCapability(Capability):
    """A capability bound to its `MCPServer`."""

    __slots__ = ("_server",)

    def __init__(self, server: MCPServer) -> None:
        self._server = server

    def _connection_can_be_told(self) -> bool:
        """Whether the connection being answered can receive a notification.

        Only a stateful connection has an outbound stream to carry the
        `listChanged` notification it promises.
        """
        return bool(self._server.connection_is_stateful())
