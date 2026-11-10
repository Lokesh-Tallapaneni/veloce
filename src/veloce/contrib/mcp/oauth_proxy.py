"""Upstream OAuth proxy, fronting a real OAuth/OIDC identity provider.

`/authorize` redirects the browser to the upstream using this proxy's own
fixed callback; the callback exchanges the upstream's code, stores its token
set, and mints a locally-signed JWT a `/token` leg hands back to the client.
`MCPAuth` verifies that JWT like any other bearer token - no new
resource-server machinery.
"""

from __future__ import annotations

import secrets
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlencode

from veloce.contrib.mcp.authorization import (
    _PKCE_S256,
    _digest,
    _error_response,
    _now,
    _redirect_error,
    _redirect_uri_is_allowed,
)
from veloce.http.response import RedirectResponse, Response
from veloce.status import HTTP_302_FOUND

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.request import Request

_WELL_KNOWN_PATH = "/.well-known/openid-configuration"
#: Entropy for the upstream-facing state and the proxy's own issued code.
_TOKEN_ENTROPY_BYTES = 32
#: Lifetime of the code this proxy hands the original client - separate from
#: whatever TTL the upstream applies to its own authorization code.
_PROXY_CODE_TTL_SECONDS = 60.0


def _httpx2() -> Any:
    """Import `httpx2`, raising the shared install hint on failure."""
    try:
        import httpx2
    except ImportError as exc:
        if sys.version_info < (3, 11):
            raise RuntimeError(
                "the OAuth proxy needs the 'httpx2' package, which requires "
                "Python >= 3.11 and cannot be installed on this interpreter."
            ) from exc
        raise RuntimeError(
            "the OAuth proxy needs the 'httpx2' package. "
            "Install it with: pip install veloceframework[mcp-oauth-proxy]"
        ) from exc
    return httpx2


@dataclass(slots=True)
class PendingAuthorization:
    """One in-flight `/authorize` request, correlated to the upstream's callback."""

    client_redirect_uri: str
    client_code_challenge: str
    client_state: str | None = None


@dataclass(slots=True)
class UpstreamTokenSet:
    """The upstream's own tokens for one subject, kept to mint and refresh ours."""

    access_token: str
    refresh_token: str | None
    expires_at: float
    claims: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProxyAuthorizationCode:
    """One code this proxy issued to the original client, redeemed at `/token`."""

    client_redirect_uri: str
    client_code_challenge: str
    jti: str
    expires_at: float


@runtime_checkable
class UpstreamOAuthProxyStore(Protocol):
    """Where pending authorize requests, issued codes, and upstream tokens live.

    The same digest-keyed shape family as `AuthorizationStore`.
    """

    async def save_pending(self, state: str, pending: PendingAuthorization) -> None:
        """Record a pending authorization under `state`."""

    async def take_pending(self, state: str) -> PendingAuthorization | None:
        """Return and remove the pending authorization stored under `state`."""

    async def save_code(self, code_digest: str, code: ProxyAuthorizationCode) -> None:
        """Record a proxy-issued authorization code under `code_digest`."""

    async def take_code(self, code_digest: str) -> ProxyAuthorizationCode | None:
        """Return and remove the code stored under `code_digest`."""

    async def save_upstream_tokens(self, jti: str, tokens: UpstreamTokenSet) -> None:
        """Record the upstream's token set under `jti`."""

    async def get_upstream_tokens(self, jti: str) -> UpstreamTokenSet | None:
        """Return the token set recorded under `jti`, or `None`."""

    async def delete_upstream_tokens(self, jti: str) -> None:
        """Remove the token set recorded under `jti`, if any."""


class InMemoryUpstreamOAuthProxyStore:
    """Single-process, dict-backed `UpstreamOAuthProxyStore` - lost on restart."""

    __slots__ = ("_codes", "_pending", "_tokens")

    def __init__(self) -> None:
        self._pending: dict[str, PendingAuthorization] = {}
        self._codes: dict[str, ProxyAuthorizationCode] = {}
        self._tokens: dict[str, UpstreamTokenSet] = {}

    async def save_pending(self, state: str, pending: PendingAuthorization) -> None:
        self._pending[state] = pending

    async def take_pending(self, state: str) -> PendingAuthorization | None:
        return self._pending.pop(state, None)

    async def save_code(self, code_digest: str, code: ProxyAuthorizationCode) -> None:
        self._codes[code_digest] = code

    async def take_code(self, code_digest: str) -> ProxyAuthorizationCode | None:
        return self._codes.pop(code_digest, None)

    async def save_upstream_tokens(self, jti: str, tokens: UpstreamTokenSet) -> None:
        self._tokens[jti] = tokens

    async def get_upstream_tokens(self, jti: str) -> UpstreamTokenSet | None:
        return self._tokens.get(jti)

    async def delete_upstream_tokens(self, jti: str) -> None:
        self._tokens.pop(jti, None)


async def discover_oidc_endpoints(issuer: str) -> tuple[str, str]:
    """Fetch `issuer`'s OIDC discovery document and return the two endpoints.

    Returns `(authorization_endpoint, token_endpoint)`. Okta, Entra, Auth0,
    and Google all publish this document - a small, optional convenience
    over naming both endpoints by hand.
    """
    httpx2 = _httpx2()
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(10.0)) as client:
        response = await client.get(issuer.rstrip("/") + _WELL_KNOWN_PATH)
        response.raise_for_status()
        document = response.json()
    return document["authorization_endpoint"], document["token_endpoint"]


@dataclass(slots=True)
class UpstreamOAuthProxy:
    """Front a real upstream OAuth/OIDC identity provider for MCP clients.

    Usage::

        from veloce.contrib.mcp import UpstreamOAuthProxy, register_oauth_proxy

        proxy = UpstreamOAuthProxy(
            issuer="https://api.example.com",
            upstream_authorization_endpoint="https://idp.example/authorize",
            upstream_token_endpoint="https://idp.example/token",
            upstream_client_id="...",
            upstream_client_secret="...",
            signing_key="...",
        )
        register_oauth_proxy(app, proxy)
    """

    #: Veloce's own issuer identity, presented to MCP clients.
    issuer: str
    upstream_authorization_endpoint: str
    upstream_token_endpoint: str
    upstream_client_id: str
    upstream_client_secret: str
    #: HMAC key for the proxy's own locally-minted JWTs.
    signing_key: str | bytes
    #: This proxy's own fixed callback, registered upstream once.
    redirect_path: str = "/oauth/callback"
    scopes_supported: Iterable[str] = ()
    access_token_ttl: float = 3600.0
    allow_dynamic_registration: bool = True
    store: UpstreamOAuthProxyStore = field(default_factory=InMemoryUpstreamOAuthProxyStore)

    @property
    def callback_url(self) -> str:
        """This proxy's fixed redirect_uri, as presented to the upstream."""
        return self.issuer.rstrip("/") + self.redirect_path


async def _handle_authorize(proxy: UpstreamOAuthProxy, request: Request) -> Response:
    """Redirect the browser to the upstream, using this proxy's own callback.

    Errors before the redirect target is trusted are shown here rather than
    redirected: sending them onward would make this proxy an open redirector.
    """
    params = request.query_params
    redirect_uri = params.get("redirect_uri")
    if not redirect_uri or not _redirect_uri_is_allowed(redirect_uri):
        return _error_response("invalid_request", "redirect_uri is missing or not allowed")

    client_state = params.get("state")
    challenge = params.get("code_challenge")
    if not challenge:
        return _redirect_error(
            redirect_uri, "invalid_request", "code_challenge is required (PKCE)", client_state
        )
    if params.get("code_challenge_method") != _PKCE_S256:
        return _redirect_error(
            redirect_uri,
            "invalid_request",
            f"code_challenge_method must be {_PKCE_S256}",
            client_state,
        )

    upstream_state = secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    await proxy.store.save_pending(
        _digest(upstream_state),
        PendingAuthorization(
            client_redirect_uri=redirect_uri,
            client_code_challenge=challenge,
            client_state=client_state,
        ),
    )
    upstream_params = {
        "response_type": "code",
        "client_id": proxy.upstream_client_id,
        "redirect_uri": proxy.callback_url,
        "state": upstream_state,
    }
    scope = " ".join(proxy.scopes_supported)
    if scope:
        upstream_params["scope"] = scope
    joiner = "&" if "?" in proxy.upstream_authorization_endpoint else "?"
    return RedirectResponse(
        f"{proxy.upstream_authorization_endpoint}{joiner}{urlencode(upstream_params)}",
        status_code=HTTP_302_FOUND,
    )


async def _handle_upstream_callback(proxy: UpstreamOAuthProxy, request: Request) -> Response:
    """Exchange the upstream's code and redirect back to the original client."""
    params = request.query_params
    upstream_state = params.get("state")
    if not upstream_state:
        return _error_response("invalid_request", "state is required")

    # Taken, not read: a failed exchange below leaves nothing stored, and a
    # replayed callback finds nothing even if the checks below would pass.
    pending = await proxy.store.take_pending(_digest(upstream_state))
    if pending is None:
        return _error_response("invalid_request", "state is unknown or has expired")

    upstream_error = params.get("error")
    if upstream_error:
        return _redirect_error(
            pending.client_redirect_uri,
            upstream_error,
            params.get("error_description") or "the upstream denied the request",
            pending.client_state,
        )
    upstream_code = params.get("code")
    if not upstream_code:
        return _redirect_error(
            pending.client_redirect_uri,
            "invalid_request",
            "the upstream did not return a code",
            pending.client_state,
        )

    httpx2 = _httpx2()
    try:
        async with httpx2.AsyncClient(timeout=httpx2.Timeout(10.0)) as client:
            response = await client.post(
                proxy.upstream_token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": upstream_code,
                    "redirect_uri": proxy.callback_url,
                    "client_id": proxy.upstream_client_id,
                    "client_secret": proxy.upstream_client_secret,
                },
            )
    except httpx2.HTTPError:
        return _redirect_error(
            pending.client_redirect_uri,
            "server_error",
            "could not reach the upstream token endpoint",
            pending.client_state,
        )
    if response.status_code >= 400:
        return _redirect_error(
            pending.client_redirect_uri,
            "server_error",
            "the upstream rejected the code exchange",
            pending.client_state,
        )
    body = response.json()

    jti = secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    await proxy.store.save_upstream_tokens(
        jti,
        UpstreamTokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at=_now() + float(body.get("expires_in", proxy.access_token_ttl)),
            claims=body,
        ),
    )

    proxy_code = secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    await proxy.store.save_code(
        _digest(proxy_code),
        ProxyAuthorizationCode(
            client_redirect_uri=pending.client_redirect_uri,
            client_code_challenge=pending.client_code_challenge,
            jti=jti,
            expires_at=_now() + _PROXY_CODE_TTL_SECONDS,
        ),
    )
    granted = {"code": proxy_code}
    if pending.client_state is not None:
        granted["state"] = pending.client_state
    joiner = "&" if "?" in pending.client_redirect_uri else "?"
    return RedirectResponse(
        f"{pending.client_redirect_uri}{joiner}{urlencode(granted)}", status_code=HTTP_302_FOUND
    )
