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

`MCPAuth.bearer` configures the other deployment: a server reached over a trusted
network that holds one static token and has neither a canonical resource URI nor
an authorization server. Tokens are still validated and refusals still carry a
challenge, but no metadata document is served, because there is none a client
could act on.

Token validation logic stays with the application (pass your own `verify`); Veloce
never implements token parsing or crypto itself, per the MCP security guidance.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from veloce._internal import _refuse_string_scopes

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

    For a static token on a trusted network, see `MCPAuth.bearer`.
    """

    # Must verify the token's audience is this server.
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
    # Static-token mode: no protected-resource metadata is served, and none is
    # required of the configuration. Set it through `MCPAuth.bearer`, which
    # carries the documented constraints.
    bearer_only: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.required_scopes, (str, bytes)):
            _refuse_string_scopes(self.required_scopes, "MCPAuth.required_scopes")
        self.required_scopes = frozenset(self.required_scopes)
        self.authorization_servers = tuple(self.authorization_servers)
        if isinstance(self.scopes_supported, (str, bytes)):
            _refuse_string_scopes(self.scopes_supported, "MCPAuth.scopes_supported")
        self.scopes_supported = tuple(self.scopes_supported)
        # Bearer-only mode serves no metadata document, so it owes neither of the
        # fields below - but a configuration asking for both modes at once is not
        # coherent, and would silently drop the OAuth fields it went to the
        # trouble of supplying rather than serve them.
        if self.bearer_only:
            if self.resource_server_url or self.authorization_servers:
                raise ValueError(
                    "MCPAuth.bearer serves no protected-resource metadata, so it "
                    "cannot carry resource_server_url or authorization_servers; "
                    "construct MCPAuth(...) directly for an OAuth resource server."
                )
            return
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

    @classmethod
    def bearer(cls, *, verify: TokenVerifier, required_scopes: Iterable[str] = ()) -> MCPAuth:
        """Validate a static bearer token without acting as an OAuth resource server.

        For a server on a trusted network - a local daemon, a sidecar, a service
        behind an internal gateway - holding one pre-shared token. There is no
        canonical resource URI to audience-bind against and no authorization
        server to obtain a token from, so no RFC 9728 protected-resource metadata
        route is registered and the challenges omit the pointer to it.

        Everything else is unchanged: a missing or rejected token is answered
        `401` and a token short of `required_scopes` is answered `403`, each with
        a `WWW-Authenticate` challenge, and `verify` still owns validation.

        Use `MCPAuth(...)` instead for anything reachable from an untrusted
        network: a pre-shared token cannot be audience-bound, so a client tricked
        into presenting it elsewhere has no protection from RFC 8707, and it
        cannot be rotated or revoked per client.

        Usage::

            app.mount_mcp(transport="http", auth=MCPAuth.bearer(
                verify=check_my_token,            # str token -> Principal | None
                required_scopes=["mcp:tools"],
            ))
        """
        return cls(verify=verify, required_scopes=required_scopes, bearer_only=True)

    def metadata(self) -> dict[str, object]:
        """Build the RFC 9728 protected-resource metadata document."""
        if self.bearer_only:
            raise RuntimeError(
                "MCPAuth.bearer has no protected-resource metadata to serve; the "
                "transport does not register the metadata route in this mode."
            )
        document: dict[str, object] = {}
        if self.resource_server_url is not None:
            document["resource"] = self.resource_server_url
        if self.authorization_servers:
            document["authorization_servers"] = list(self.authorization_servers)
        if self.scopes_supported:
            document["scopes_supported"] = list(self.scopes_supported)
        # RFC 9728 section 2: how the client is to present its token. The transport
        # reads the `Authorization` header and nothing else - the MCP authorization
        # spec forbids carrying an access token in the query string - so a client
        # reading this document is told exactly what will be accepted.
        document["bearer_methods_supported"] = ["header"]
        return document
