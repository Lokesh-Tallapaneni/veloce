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

    #: Methods of this capability that the modern revision retired. The server
    #: refuses them with method-not-found there, so a client discovers the
    #: surface it actually has.
    #:
    #: Declared here rather than in a name table in the dispatcher: withholding
    #: a method from `advertise(modern=True)` and refusing it at dispatch are two
    #: halves of one rule, and split across two files a capability author edits
    #: one and the server then advertises what it refuses.
    handshake_only_methods: frozenset[str] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # A slotted base whose subclass forgets `__slots__` silently regains a
        # per-instance `__dict__`; fail loudly so the discipline is structural.
        super().__init_subclass__(**kwargs)
        if "__slots__" not in cls.__dict__:
            raise TypeError(f"{cls.__name__} must declare __slots__")

    def advertise(self, *, modern: bool = False) -> dict[str, Any] | None:
        """Return this capability's entry for the `initialize` capabilities object.

        A single-key mapping (e.g. ``{"tools": {"listChanged": False}}``) merged
        into the advertised capabilities, or `None` when the app exposes nothing
        for this area so the client does not probe an empty primitive.

        Declare a `modern` keyword parameter to vary the entry by protocol
        revision - the server passes it when the signature accepts it, so a
        capability whose methods a revision retired can withhold or narrow what
        it advertises rather than promising something the dispatcher refuses.
        """
        raise NotImplementedError

    def handlers(self) -> dict[str, MethodHandler]:
        """Return the ``{json_rpc_method: handler}`` map this capability answers."""
        raise NotImplementedError

    def extensions(self) -> dict[str, Any] | None:
        """Return this capability's entry for the advertised extensions object.

        `None` - the default - means this capability contributes no extension,
        which is true of most of them. A capability contributes an entry only
        when the feature it names is actually available, so a server with no
        task-capable tool advertises no tasks extension and a client will never
        offer one.

        Declared here rather than probed for. The server used to resolve this by
        `getattr(capability, "extensions", None)`, so the third member of the
        contract was invisible to anyone reading the base class - and this class
        is the documented seam an out-of-tree capability implements against.
        """
        return None


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
