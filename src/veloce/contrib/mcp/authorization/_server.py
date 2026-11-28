"""The authorization server — configuration, token issue and route registration."""

from __future__ import annotations

import secrets
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from veloce._internal import _refuse_string_scopes
from veloce._protocol_constants import HTTP_METHOD_GET, HTTP_METHOD_POST
from veloce.contrib.mcp.authorization._endpoints import (
    _handle_authorize,
    _handle_register,
    _handle_token,
)
from veloce.contrib.mcp.authorization._records import (
    _CREDENTIAL_ENTROPY_BYTES,
    _LOOPBACK_HOSTS,
    _PKCE_S256,
    AUTHORIZATION_SERVER_METADATA_PATH,
    AccessToken,
    AuthorizationStore,
    InMemoryAuthorizationStore,
    _digest,
    _now,
)
from veloce.http.response import JSONResponse, Response
from veloce.principal import Principal

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Sequence

    from veloce.http.request import Request

# Returns the principal to issue for, `None` to refuse, or a `Response` to send.
Authenticator = Callable[
    ["Request"], "Principal | Response | None | Awaitable[Principal | Response | None]"
]


@dataclass(slots=True)
class MCPAuthorizationServer:
    """An OAuth 2.1 authorization server for MCP clients.

    Usage::

        from veloce.contrib.mcp import (
            MCPAuth, MCPAuthorizationServer, register_authorization_server,
        )

        def authenticate(request):
            user = request.session.get("user")
            if user is None:
                return RedirectResponse(f"/login?next={request.url}")
            return Principal(subject=user, scopes={"mcp:tools"})

        authorization = MCPAuthorizationServer(
            issuer="https://api.example.com",
            authenticate=authenticate,
            scopes_supported=["mcp:tools"],
        )
        register_authorization_server(app, authorization)

        app.mount_mcp(transport="http", auth=MCPAuth(
            verify=authorization.verifier(),
            resource_server_url="https://api.example.com/mcp",
            authorization_servers=["https://api.example.com"],
        ))
    """

    issuer: str
    """The base every endpoint is advertised under: the https origin clients reach."""
    authenticate: Authenticator
    """Establishes who is approving access. See `Authenticator`."""
    scopes_supported: Iterable[str] = ()
    """Advertised in the metadata; a request outside this set is refused."""
    store: AuthorizationStore = field(default_factory=InMemoryAuthorizationStore)
    """Where clients, codes and tokens live."""
    access_token_ttl: float = 3600.0
    """Lifetime of an issued access token."""
    allow_dynamic_registration: bool = True
    """Whether `POST /register` issues credentials to anyone (RFC 7591)."""

    def __post_init__(self) -> None:
        if isinstance(self.scopes_supported, (str, bytes)):
            _refuse_string_scopes(self.scopes_supported, "MCPAuthorizationServer.scopes_supported")
        self.scopes_supported = tuple(self.scopes_supported)
        if not self.issuer:
            raise ValueError("MCPAuthorizationServer requires an issuer URL")
        # Tokens are minted under the issuer, so a cleartext one leaks them all.
        parsed = urlparse(self.issuer)
        if parsed.scheme != "https" and parsed.hostname not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"MCPAuthorizationServer issuer {self.issuer!r} must be https "
                "(http is allowed only on the loopback interface, for development)."
            )
        if self.access_token_ttl <= 0:
            raise ValueError("access_token_ttl must be a positive number of seconds")

    # ── Discovery ─────────────────────────────────────────

    def metadata(self) -> dict[str, Any]:
        """Build the RFC 8414 authorization server metadata document."""
        document: dict[str, Any] = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer.rstrip('/')}/authorize",
            "token_endpoint": f"{self.issuer.rstrip('/')}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            # Advertised, so a client reading this knows not to offer `plain`.
            "code_challenge_methods_supported": [_PKCE_S256],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        }
        if self.scopes_supported:
            document["scopes_supported"] = list(self.scopes_supported)
        if self.allow_dynamic_registration:
            document["registration_endpoint"] = f"{self.issuer.rstrip('/')}/register"
        return document

    # ── Issuing ───────────────────────────────────────────

    def verifier(
        self, *, resource: str | None = None
    ) -> Callable[[str], Awaitable[Principal | None]]:
        """Return the token verifier to hand `MCPAuth(verify=...)`.

        Resolves an opaque token to its `Principal`, refusing one that has expired
        or that was minted for a different resource.

        `resource` is this server's canonical URI, the same value passed as
        `MCPAuth(resource_server_url=...)`. Given it, a token whose RFC 8707
        `resource` names a different server is refused - that is the audience
        binding, and it is what stops a token obtained for one MCP server being
        replayed against another sharing the authorization server. Omitting it
        leaves the check off and warns, since it cannot be performed without
        knowing which server this is.
        """
        if resource is None:
            warnings.warn(
                "MCPAuthorizationServer.verifier() was built without resource=, so "
                "audience binding (RFC 8707) is not enforced and a token minted for "
                "another MCP server sharing this authorization server is accepted. "
                "Pass the same URI given as MCPAuth(resource_server_url=...).",
                stacklevel=2,
            )

        async def verify(token: str) -> Principal | None:
            record = await self.store.get_token(_digest(token))
            if record is None:
                return None
            if record.expires_at <= _now():
                await self.store.delete_token(_digest(token))
                return None
            # `resource` is optional at `/authorize` (RFC 8707 Sec. 2), so an
            # unbound token is refused too: exempting it would let a client reach
            # every server sharing this authorization server by asking for less.
            if resource is not None and record.resource != resource:
                return None
            return Principal(
                subject=record.subject,
                scopes=record.scopes,
                claims={**record.claims, "client_id": record.client_id, "aud": record.resource},
                token=token,
            )

        return verify

    async def _issue(
        self,
        client_id: str,
        subject: str | None,
        scopes: frozenset[str],
        resource: str | None,
        claims: dict[str, Any],
        family_id: str | None = None,
    ) -> dict[str, Any]:
        """Mint an access/refresh pair and return the RFC 6749 token response.

        `family_id` carries the chain forward on a rotation, so a later replay of
        any refresh token in it revokes the whole chain. Omitted for a first
        issuance, which starts a family of its own.
        """
        access = secrets.token_urlsafe(_CREDENTIAL_ENTROPY_BYTES)
        refresh = secrets.token_urlsafe(_CREDENTIAL_ENTROPY_BYTES)
        record = AccessToken(
            client_id=client_id,
            subject=subject,
            scopes=scopes,
            resource=resource,
            expires_at=_now() + self.access_token_ttl,
            claims=claims,
            refresh_digest=_digest(refresh),
            **({"family_id": family_id} if family_id is not None else {}),
        )
        await self.store.save_token(_digest(access), record)
        response: dict[str, Any] = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": int(self.access_token_ttl),
            "refresh_token": refresh,
        }
        if scopes:
            response["scope"] = " ".join(sorted(scopes))
        return response


def register_authorization_server(
    app: Any,
    server: MCPAuthorizationServer,
    prefix: str = "",
    exclude_middleware: Sequence[str] | None = None,
) -> None:
    """Mount `server`'s OAuth endpoints on `app`.

    Registers the RFC 8414 metadata document, `/authorize`, `/token`, and - when
    dynamic registration is on - `/register`. The metadata advertises whatever
    the issuer says, so `prefix` and the issuer must agree.
    """
    base = prefix.rstrip("/")

    async def metadata(request: Request) -> Response:
        return JSONResponse(server.metadata(), headers={"Cache-Control": "no-store"})

    async def authorize(request: Request) -> Response:
        return await _handle_authorize(server, request)

    async def token(request: Request) -> Response:
        return await _handle_token(server, request)

    async def register(request: Request) -> Response:
        return await _handle_register(server, request)

    app.add_route(
        AUTHORIZATION_SERVER_METADATA_PATH,
        metadata,
        methods=[HTTP_METHOD_GET],
        include_in_schema=False,
        exclude_middleware=exclude_middleware,
    )
    app.add_route(
        f"{base}/authorize",
        authorize,
        methods=[HTTP_METHOD_GET],
        include_in_schema=False,
        exclude_middleware=exclude_middleware,
    )
    app.add_route(
        f"{base}/token",
        token,
        methods=[HTTP_METHOD_POST],
        include_in_schema=False,
        exclude_middleware=exclude_middleware,
    )
    if server.allow_dynamic_registration:
        app.add_route(
            f"{base}/register",
            register,
            methods=[HTTP_METHOD_POST],
            include_in_schema=False,
            exclude_middleware=exclude_middleware,
        )
