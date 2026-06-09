"""MCP transport authentication — OAuth 2.1 Resource Server validation.

`MCPAuth` configures token validation for the Streamable HTTP transport. The MCP
server acts as an OAuth 2.1 *resource server*: every request to the mounted route
must carry a `Bearer` token, which a user-supplied `verify` callable validates
(checking signature, expiry, and - critically - that the token's audience is this
server, per RFC 8707). A valid token yields a `Principal` (identity + scopes) that
is published via `set_principal` for the duration of the request; a missing or
invalid token yields `401`, and a token lacking the endpoint's required scopes
yields `403`, each with a `WWW-Authenticate` challenge pointing at the server's
RFC 9728 protected-resource metadata.

Token validation logic stays with the application (pass your own `verify`); Veloce
never implements token parsing or crypto itself, per the MCP security guidance.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from veloce.principal import Principal

# A token verifier returns the authenticated `Principal`, or `None` to reject. It
# may be sync or async.
TokenVerifier = Callable[[str], "Principal | None | Awaitable[Principal | None]"]

# RFC 9728 well-known path for OAuth 2.0 Protected Resource Metadata.
PROTECTED_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"


@dataclass(slots=True)
class MCPAuth:
    """OAuth 2.1 Resource Server configuration for the MCP HTTP transport.

    Usage::

        app.mount_mcp(transport="http", auth=MCPAuth(
            verify=my_token_verifier,            # str token -> Principal | None
            required_scopes=["mcp:tools"],
            resource_server_url="https://api.example.com/mcp",
            authorization_servers=["https://auth.example.com"],
        ))
    """

    # Validates a bearer token and returns the authenticated `Principal`, or
    # `None` to reject. Must verify the token's audience is this server.
    verify: TokenVerifier
    # Scopes every request to the MCP endpoint must carry (a `403` otherwise).
    # Per-tool scopes (`@app.mcp_tool(scopes=...)`) are checked additionally.
    required_scopes: Iterable[str] = ()
    # The canonical resource URI of this MCP server (RFC 8707 / RFC 9728), echoed
    # in the protected-resource metadata as `resource`.
    resource_server_url: str | None = None
    # Authorization server issuer URLs advertised in the metadata so a client can
    # discover where to obtain a token.
    authorization_servers: Iterable[str] = ()
    # Scopes advertised in the metadata as available for this resource.
    scopes_supported: Iterable[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.required_scopes = frozenset(self.required_scopes)
        self.authorization_servers = tuple(self.authorization_servers)
        self.scopes_supported = tuple(self.scopes_supported)
        # The MCP authorization spec requires the protected-resource metadata to
        # carry the canonical resource URI (so a client can audience-bind its token
        # per RFC 8707) and at least one authorization server (so it can discover
        # where to obtain a token). Enforce both, rather than serving an incomplete
        # metadata document a compliant client cannot act on.
        if not self.resource_server_url:
            raise ValueError(
                "MCPAuth requires resource_server_url (the canonical MCP server "
                "URI), so the token audience can be bound to this server."
            )
        if not self.authorization_servers:
            raise ValueError(
                "MCPAuth requires at least one authorization_servers entry, so a "
                "client can discover where to obtain a token (RFC 9728)."
            )

    def metadata(self) -> dict[str, object]:
        """Build the RFC 9728 protected-resource metadata document."""
        document: dict[str, object] = {}
        if self.resource_server_url is not None:
            document["resource"] = self.resource_server_url
        if self.authorization_servers:
            document["authorization_servers"] = list(self.authorization_servers)
        if self.scopes_supported:
            document["scopes_supported"] = list(self.scopes_supported)
        return document
