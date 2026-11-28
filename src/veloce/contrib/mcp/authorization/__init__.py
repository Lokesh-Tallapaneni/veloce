"""OAuth 2.1 authorization server — issue the tokens an MCP client comes for.

`MCPAuth` makes an MCP endpoint a *resource server*, validating a bearer token
someone else issued. This module is the other half, for a deployment with no
identity provider to delegate to: it issues those tokens itself and publishes
the discovery documents an MCP client walks to find it.

What it implements, and what it deliberately does not:

- **Opaque tokens, not signed ones.** A token is 32 bytes of `secrets` entropy,
  stored as a SHA-256 digest and answered by lookup, so there is no signing key
  to rotate, no algorithm to confuse, and a stolen database yields digests
  rather than usable credentials. The cost is a store lookup per validation.
- **Authenticating the user is the application's job.** `authenticate` is called
  with the authorization request and returns the `Principal` to issue for, or a
  `Response` (a login page, a consent screen) returned to the browser instead.
- **PKCE is required, S256 only** (OAuth 2.1). A request without a challenge, or
  asking for `plain`, is refused: this server issues to public clients, where a
  code intercepted on the redirect is the whole attack.
- **Audience binding.** The `resource` parameter (RFC 8707) is recorded on the
  token and checked by `verifier(resource=...)`, so a token minted for one MCP
  server cannot be replayed against another. Building the verifier without
  `resource=` leaves the check off and warns.

Storage is behind `AuthorizationStore`; the bundled `InMemoryAuthorizationStore`
is single-process and lost on restart.
"""

from __future__ import annotations

from veloce.contrib.mcp.authorization._endpoints import (
    SUPPORTED_GRANT_TYPES,
    _authenticate_client,
    _error_response,
    _grant_authorization_code,
    _grant_refresh_token,
    _handle_authorize,
    _handle_register,
    _handle_token,
    _redirect_error,
    _redirect_uri_is_allowed,
    _refuse_unregistered_grant,
    _revoke_family,
    _spent_family,
    _verify_pkce,
)
from veloce.contrib.mcp.authorization._records import (
    _CODE_TTL_SECONDS,
    _CREDENTIAL_ENTROPY_BYTES,
    _LOOPBACK_HOSTS,
    _PKCE_S256,
    AUTHORIZATION_SERVER_METADATA_PATH,
    AccessToken,
    AuthorizationCode,
    AuthorizationStore,
    InMemoryAuthorizationStore,
    OAuthClient,
    _digest,
    _now,
)
from veloce.contrib.mcp.authorization._server import (
    Authenticator,
    MCPAuthorizationServer,
    register_authorization_server,
)

__all__ = [
    "AUTHORIZATION_SERVER_METADATA_PATH",
    "AccessToken",
    "Authenticator",
    "AuthorizationCode",
    "AuthorizationStore",
    "InMemoryAuthorizationStore",
    "MCPAuthorizationServer",
    "OAuthClient",
    "SUPPORTED_GRANT_TYPES",
    "_CODE_TTL_SECONDS",
    "_CREDENTIAL_ENTROPY_BYTES",
    "_LOOPBACK_HOSTS",
    "_PKCE_S256",
    "_authenticate_client",
    "_digest",
    "_error_response",
    "_grant_authorization_code",
    "_grant_refresh_token",
    "_handle_authorize",
    "_handle_register",
    "_handle_token",
    "_now",
    "_redirect_error",
    "_redirect_uri_is_allowed",
    "_refuse_unregistered_grant",
    "_revoke_family",
    "_spent_family",
    "_verify_pkce",
    "register_authorization_server",
]
