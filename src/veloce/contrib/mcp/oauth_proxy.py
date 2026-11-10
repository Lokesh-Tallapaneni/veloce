"""Upstream OAuth proxy data model, storage protocol, and OIDC discovery.

Fronts a real upstream OAuth/OIDC identity provider for an MCP client: a
later authorize/callback leg exchanges the upstream's code and mints a
locally-signed JWT a token leg hands back to the client. This module holds
only the shapes that round trip through a store between those two legs - no
HTTP handlers, no JWT minting.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_WELL_KNOWN_PATH = "/.well-known/openid-configuration"


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


@runtime_checkable
class UpstreamOAuthProxyStore(Protocol):
    """Where pending authorize requests and upstream token sets live."""

    async def save_pending(self, key: str, pending: PendingAuthorization) -> None:
        """Record a pending authorization under `key`."""

    async def take_pending(self, key: str) -> PendingAuthorization | None:
        """Return and remove the pending authorization stored under `key`."""

    async def save_upstream_tokens(self, key: str, tokens: UpstreamTokenSet) -> None:
        """Record the upstream's token set under `key`."""

    async def get_upstream_tokens(self, key: str) -> UpstreamTokenSet | None:
        """Return the token set recorded under `key`, or `None`."""

    async def delete_upstream_tokens(self, key: str) -> None:
        """Remove the token set recorded under `key`, if any."""


class InMemoryUpstreamOAuthProxyStore:
    """Single-process, dict-backed `UpstreamOAuthProxyStore` - lost on restart."""

    __slots__ = ("_pending", "_tokens")

    def __init__(self) -> None:
        self._pending: dict[str, PendingAuthorization] = {}
        self._tokens: dict[str, UpstreamTokenSet] = {}

    async def save_pending(self, key: str, pending: PendingAuthorization) -> None:
        self._pending[key] = pending

    async def take_pending(self, key: str) -> PendingAuthorization | None:
        return self._pending.pop(key, None)

    async def save_upstream_tokens(self, key: str, tokens: UpstreamTokenSet) -> None:
        self._tokens[key] = tokens

    async def get_upstream_tokens(self, key: str) -> UpstreamTokenSet | None:
        return self._tokens.get(key)

    async def delete_upstream_tokens(self, key: str) -> None:
        self._tokens.pop(key, None)


async def discover_oidc_endpoints(issuer: str) -> tuple[str, str]:
    """Fetch `issuer`'s OIDC discovery document and return the two endpoints.

    Returns `(authorization_endpoint, token_endpoint)`. Okta, Entra, Auth0,
    and Google all publish this document - a small, optional convenience
    over naming both endpoints by hand.
    """
    try:
        import httpx2
    except ImportError as exc:
        if sys.version_info < (3, 11):
            raise RuntimeError(
                "the OAuth proxy needs the 'httpx2' package, which requires "
                "Python >= 3.11 and cannot be installed on this interpreter. "
                "Name both endpoints explicitly instead, or run on 3.11+."
            ) from exc
        raise RuntimeError(
            "the OAuth proxy needs the 'httpx2' package. "
            "Install it with: pip install veloceframework[mcp-oauth-proxy]"
        ) from exc
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(10.0)) as client:
        response = await client.get(issuer.rstrip("/") + _WELL_KNOWN_PATH)
        response.raise_for_status()
        document = response.json()
    return document["authorization_endpoint"], document["token_endpoint"]
