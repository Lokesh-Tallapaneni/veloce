"""Authorization records and stores — clients, codes, tokens and where they live."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

AUTHORIZATION_SERVER_METADATA_PATH: str = "/.well-known/oauth-authorization-server"
"""RFC 8414 well-known path for authorization server metadata."""

# 256 bits, which is what a token that is the whole credential needs.
_CREDENTIAL_ENTROPY_BYTES = 32

# One hop, browser to client, so the window is that round trip. OAuth 2.1
# allows ten minutes.
_CODE_TTL_SECONDS = 60.0

# `plain` would put the verifier on the wire, defeating the exchange.
_PKCE_S256 = "S256"

# OAuth 2.1 forbids a cleartext redirect anywhere but the loopback interface.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _digest(value: str) -> str:
    """Return the stored form of a credential: never the credential itself."""
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> float:
    return time.time()


@dataclass(slots=True)
class OAuthClient:
    """A registered client: who may redirect where, and how it authenticates."""

    client_id: str
    redirect_uris: tuple[str, ...]
    client_secret_digest: str | None = None
    """`None` for a public client, which proves itself with PKCE alone."""
    client_name: str | None = None
    scopes: frozenset[str] = frozenset()
    grant_types: tuple[str, ...] = ("authorization_code", "refresh_token")

    @property
    def is_public(self) -> bool:
        """Whether this client has no secret and relies on PKCE."""
        return self.client_secret_digest is None


@dataclass(slots=True)
class AuthorizationCode:
    """One issued code, bound to everything it was issued for."""

    client_id: str
    redirect_uri: str
    code_challenge: str
    subject: str | None
    scopes: frozenset[str]
    resource: str | None
    expires_at: float
    claims: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AccessToken:
    """One issued token: who it is for, what it may do, and until when."""

    client_id: str
    subject: str | None
    scopes: frozenset[str]
    resource: str | None
    expires_at: float
    claims: dict[str, Any] = field(default_factory=dict)
    refresh_digest: str | None = None
    """Set when refresh was granted; rotation issues a new pair and drops the old."""
    family_id: str = field(default_factory=lambda: secrets.token_urlsafe(9))
    """The chain this token descends from, inherited by every rotation. A refresh token presented twice
    means a copy leaked, and OAuth 2.1 Sec. 4.14.2 wants the whole chain revoked - refusing only the
    replay leaves the thief the pair they already rotated.
    """


@runtime_checkable
class AuthorizationStore(Protocol):
    """Where issued clients, codes and tokens live.

    Keyed by digest, never by the credential, so a leaked store yields nothing
    usable. `take_code` is single-use by contract: it must return a code at most
    once, so a replayed code finds nothing.
    """

    async def save_client(self, client: OAuthClient) -> None:
        """Record a newly registered client."""
        ...

    async def get_client(self, client_id: str) -> OAuthClient | None:
        """Return the registered client, or `None`."""
        ...

    async def save_code(self, code_digest: str, code: AuthorizationCode) -> None:
        """Record an issued authorization code under its digest."""
        ...

    async def take_code(self, code_digest: str) -> AuthorizationCode | None:
        """Return and *remove* the code, so a second redemption finds nothing."""
        ...

    async def save_token(self, token_digest: str, token: AccessToken) -> None:
        """Record an issued access token under its digest."""
        ...

    async def get_token(self, token_digest: str) -> AccessToken | None:
        """Return the token recorded under `token_digest`, or `None`."""
        ...

    async def delete_token(self, token_digest: str) -> None:
        """Drop a token, whether or not it was present."""
        ...

    async def take_refresh(self, refresh_digest: str) -> tuple[str, AccessToken] | None:
        """Return and remove the `(token_digest, token)` a refresh token replaces."""
        ...


class InMemoryAuthorizationStore:
    """A single-process store, for development and for tests.

    Nothing survives a restart or is shared between workers; a deployment that
    needs either supplies its own `AuthorizationStore`.
    """

    __slots__ = ("_clients", "_codes", "_tokens", "_refresh", "_spent")

    def __init__(self) -> None:
        self._clients: dict[str, OAuthClient] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._tokens: dict[str, AccessToken] = {}
        self._refresh: dict[str, str] = {}
        # Spent digests mapped to their family, never the token itself.
        self._spent: dict[str, str] = {}

    async def save_client(self, client: OAuthClient) -> None:
        """Store a registered client under its id."""
        self._clients[client.client_id] = client

    async def get_client(self, client_id: str) -> OAuthClient | None:
        """Return the client registered under `client_id`, or `None`."""
        return self._clients.get(client_id)

    async def save_code(self, code_digest: str, code: AuthorizationCode) -> None:
        """Store an authorization code under its digest."""
        self._codes[code_digest] = code

    async def take_code(self, code_digest: str) -> AuthorizationCode | None:
        """Remove and return the code stored under `code_digest`, or `None`."""
        return self._codes.pop(code_digest, None)

    async def save_token(self, token_digest: str, token: AccessToken) -> None:
        """Store an access token under its digest, indexing its refresh digest."""
        self._tokens[token_digest] = token
        if token.refresh_digest is not None:
            self._refresh[token.refresh_digest] = token_digest

    async def get_token(self, token_digest: str) -> AccessToken | None:
        """Return the access token stored under `token_digest`, or `None`."""
        return self._tokens.get(token_digest)

    async def delete_token(self, token_digest: str) -> None:
        """Remove an access token and the refresh index entry pointing at it."""
        token = self._tokens.pop(token_digest, None)
        if token is not None and token.refresh_digest is not None:
            self._refresh.pop(token.refresh_digest, None)

    async def take_refresh(self, refresh_digest: str) -> tuple[str, AccessToken] | None:
        """Remove a refresh token and return its access token digest and token, or `None`."""
        token_digest = self._refresh.pop(refresh_digest, None)
        if token_digest is None:
            return None
        token = self._tokens.pop(token_digest, None)
        if token is None:
            return None
        # Makes a second presentation distinguishable from a token never issued.
        self._spent[refresh_digest] = token.family_id
        return token_digest, token

    async def family_of_spent_refresh(self, refresh_digest: str) -> str | None:
        """Return the family a already-spent refresh token belonged to."""
        return self._spent.get(refresh_digest)

    async def revoke_family(self, family_id: str) -> int:
        """Drop every token descended from one authorization. Returns the count."""
        doomed = [digest for digest, token in self._tokens.items() if token.family_id == family_id]
        for digest in doomed:
            token = self._tokens.pop(digest, None)
            if token is not None and token.refresh_digest is not None:
                self._refresh.pop(token.refresh_digest, None)
        self._spent = {
            digest: family for digest, family in self._spent.items() if family != family_id
        }
        return len(doomed)
