"""Transport contract — the structural interface every MCP transport satisfies.

A transport carries JSON-RPC messages between a client and the `MCPServer`. The
server depends on this interface, not on a concrete transport, so it pushes a
one-way notification through `send` without knowing whether the wire is stdio or
HTTP. `BidirectionalTransport` extends the contract with `request` for the
server->client calls (`sampling` / `elicitation` / `roots`) that need reply
correlation; it is satisfied once a transport can await a correlated reply.

The receive loop is intentionally not part of the contract: it is transport
specific (a blocking stdin reader versus one HTTP request), so each transport
owns its own loop and converges on `MCPServer.handle_message`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """Carry one-way JSON-RPC messages from the server to the client."""

    async def send(self, message: dict[str, Any]) -> None:
        """Write one JSON-RPC message (a notification or a response) to the client."""
        ...


@runtime_checkable
class BidirectionalTransport(Transport, Protocol):
    """A transport that can also issue server->client requests and await replies."""

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Issue a server->client request and await its correlated result.

        The transport owns id minting and correlation, so it takes the method and
        params (not a pre-built message) and returns the reply's `result`.
        """
        ...
