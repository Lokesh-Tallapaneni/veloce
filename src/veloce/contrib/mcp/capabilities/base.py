"""Capability base — one MCP spec area's advertisement and method handlers.

The server *holds* a list of capability objects rather than subclassing per
feature (mirroring Veloce's mixin composition): each capability owns one spec
area, declaring what `initialize` advertises for it and which JSON-RPC methods
it answers. `initialize` builds its `capabilities` object by calling
`advertise()` over the held capabilities and dropping the `None`s; the dispatch
map is built by merging every capability's `handlers()`. A new spec area is a
new capability registered on the server, not a new branch in a dispatcher.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.server import MCPServer, MethodHandler


class Capability:
    """One MCP spec area: its `initialize` advertisement and its method handlers."""

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # A slotted base whose subclass forgets `__slots__` silently regains a
        # per-instance `__dict__`; fail loudly so the discipline is structural.
        super().__init_subclass__(**kwargs)
        if "__slots__" not in cls.__dict__:
            raise TypeError(f"{cls.__name__} must declare __slots__")

    def advertise(self) -> dict[str, Any] | None:
        """Return this capability's entry for the `initialize` capabilities object.

        A single-key mapping (e.g. ``{"tools": {"listChanged": False}}``) merged
        into the advertised capabilities, or `None` when the app exposes nothing
        for this area so the client does not probe an empty primitive.
        """
        raise NotImplementedError

    def handlers(self) -> dict[str, MethodHandler]:
        """Return the ``{json_rpc_method: handler}`` map this capability answers."""
        raise NotImplementedError


class _ServerCapability(Capability):
    """A capability bound to its `MCPServer`.

    Every concrete capability is constructed with the server and reaches the
    handlers and registries through it, so the `_server`-only slot and its
    `__init__` live here once rather than on each subclass.
    """

    __slots__ = ("_server",)

    def __init__(self, server: MCPServer) -> None:
        self._server = server

    def _connection_can_be_told(self) -> bool:
        """Whether the connection being answered can receive a notification.

        `listChanged` promises the client will be told when a list changes, and
        the only channel for that is a stateful connection's outbound stream. A
        stateless request has none, and its list cannot change anyway: nothing
        survives the response.
        """
        return bool(self._server.connection_is_stateful())
