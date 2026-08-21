"""OAuth 2.1 authorization server — issue the tokens an MCP client comes for.

`MCPAuth` makes an MCP endpoint a *resource server*: it validates a bearer token
someone else issued. This module is the other half, for a deployment with no
identity provider to delegate to: it issues those tokens itself, and publishes the
discovery documents an MCP client walks to find it.

What it implements, and what it deliberately does not:

- **Opaque tokens, not signed ones.** A token is 32 bytes of `secrets` entropy,
  stored as a SHA-256 digest and answered by lookup. There is no signing key to
  manage or rotate, no algorithm to confuse, and a stolen database yields digests
  rather than usable credentials. The cost is that validation is a store lookup
  rather than an offline signature check - the right trade for a server that is
  already answering the request.
- **Authenticating the user is the application's job.** An authorization server
  must know *who* is approving access, and only the application knows how its
  users log in. `authenticate` is called with the authorization request and
  returns the `Principal` to issue for - or a `Response` (a redirect to a login
  page, a consent screen) that is returned to the browser instead.
- **PKCE is required, S256 only** (OAuth 2.1). A request without a challenge, or
  asking for `plain`, is refused: this server issues to public clients, where a
  code intercepted on the redirect is the whole attack.
- **Audience binding.** The `resource` parameter (RFC 8707) is recorded on the
  token and checked on validation, so a token minted for one MCP server cannot be
  replayed against another.

Storage is behind `AuthorizationStore`; the bundled `InMemoryAuthorizationStore`
is for a single process and is lost on restart. Anything durable is the
application's to supply.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlencode, urlparse

from veloce._protocol_constants import HTTP_METHOD_GET, HTTP_METHOD_POST
from veloce.http.response import JSONResponse, RedirectResponse, Response
from veloce.principal import Principal
from veloce.status import HTTP_302_FOUND

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Sequence

    from veloce.http.request import Request

# RFC 8414 well-known path for authorization server metadata.
AUTHORIZATION_SERVER_METADATA_PATH = "/.well-known/oauth-authorization-server"

# Bytes of entropy per issued credential. 32 bytes is 256 bits, which is what a
# bearer token that is the whole credential needs.
_CREDENTIAL_ENTROPY_BYTES = 32

# How long an authorization code may be redeemed for. The code makes one hop from
# the browser to the client, so the window is the round trip and nothing more
# (OAuth 2.1 recommends at most ten minutes; a minute is ample and much tighter).
_CODE_TTL_SECONDS = 60.0

# The only PKCE method this server accepts. `plain` puts the verifier on the wire
# in the authorization request, which defeats the point of the exchange.
_PKCE_S256 = "S256"

# Loopback hosts a native client may redirect to over plain http. Everything else
# must be https - OAuth 2.1 forbids a cleartext redirect back to the internet.
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
    # SHA-256 of the issued secret for a confidential client; `None` for a public
    # client, which proves itself with PKCE alone.
    client_secret_digest: str | None = None
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
    # Digest of the refresh token that may replace this one, when refresh was
    # granted. Rotation issues a new pair and drops the old.
    refresh_digest: str | None = None


@runtime_checkable
class AuthorizationStore(Protocol):
    """Where issued clients, codes and tokens live.

    Codes and tokens are keyed by digest, never by the credential, so a store that
    leaks yields nothing usable. `take_code` is single-use by contract: it must
    return a code at most once, so a replayed code finds nothing.
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

    Everything is lost on restart, and nothing is shared between workers. A
    deployment that survives either needs its own `AuthorizationStore`.
    """

    __slots__ = ("_clients", "_codes", "_tokens", "_refresh")

    def __init__(self) -> None:
        self._clients: dict[str, OAuthClient] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._tokens: dict[str, AccessToken] = {}
        self._refresh: dict[str, str] = {}

    async def save_client(self, client: OAuthClient) -> None:
        self._clients[client.client_id] = client

    async def get_client(self, client_id: str) -> OAuthClient | None:
        return self._clients.get(client_id)

    async def save_code(self, code_digest: str, code: AuthorizationCode) -> None:
        self._codes[code_digest] = code

    async def take_code(self, code_digest: str) -> AuthorizationCode | None:
        return self._codes.pop(code_digest, None)

    async def save_token(self, token_digest: str, token: AccessToken) -> None:
        self._tokens[token_digest] = token
        if token.refresh_digest is not None:
            self._refresh[token.refresh_digest] = token_digest

    async def get_token(self, token_digest: str) -> AccessToken | None:
        return self._tokens.get(token_digest)

    async def delete_token(self, token_digest: str) -> None:
        token = self._tokens.pop(token_digest, None)
        if token is not None and token.refresh_digest is not None:
            self._refresh.pop(token.refresh_digest, None)

    async def take_refresh(self, refresh_digest: str) -> tuple[str, AccessToken] | None:
        token_digest = self._refresh.pop(refresh_digest, None)
        if token_digest is None:
            return None
        token = self._tokens.pop(token_digest, None)
        if token is None:
            return None
        return token_digest, token


# The `authenticate` callback: given the authorization request, return the
# principal to issue for, `None` to refuse, or a `Response` to send instead (a
# redirect to a login page, a consent screen).
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

    # The issuer identifier, and the base every endpoint is advertised under. It
    # must be the https origin clients reach this server on.
    issuer: str
    # Establishes who is approving access. See `Authenticator`.
    authenticate: Authenticator
    # Scopes this server will issue, advertised in its metadata. A request for
    # anything outside this set is refused.
    scopes_supported: Iterable[str] = ()
    # Where clients, codes and tokens live.
    store: AuthorizationStore = field(default_factory=InMemoryAuthorizationStore)
    # Lifetime of an issued access token.
    access_token_ttl: float = 3600.0
    # Whether `POST /register` issues client credentials to anyone who asks
    # (RFC 7591). An MCP client with no pre-arranged registration needs this;
    # a deployment with known clients should leave it off.
    allow_dynamic_registration: bool = True

    def __post_init__(self) -> None:
        self.scopes_supported = tuple(self.scopes_supported)
        if not self.issuer:
            raise ValueError("MCPAuthorizationServer requires an issuer URL")
        # The issuer is what a client trusts and what tokens are minted under, so
        # a cleartext one outside the loopback interface would put every token on
        # the wire in the clear.
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
            # S256 only, and advertised as such: a client that reads this knows
            # not to offer `plain`.
            "code_challenge_methods_supported": [_PKCE_S256],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        }
        if self.scopes_supported:
            document["scopes_supported"] = list(self.scopes_supported)
        if self.allow_dynamic_registration:
            document["registration_endpoint"] = f"{self.issuer.rstrip('/')}/register"
        return document

    # ── Issuing ───────────────────────────────────────────

    def verifier(self) -> Callable[[str], Awaitable[Principal | None]]:
        """Return the token verifier to hand `MCPAuth(verify=...)`.

        Resolves an opaque token to its `Principal`, refusing one that has expired
        or was minted for a different resource.
        """

        async def verify(token: str, resource: str | None = None) -> Principal | None:
            record = await self.store.get_token(_digest(token))
            if record is None:
                return None
            if record.expires_at <= _now():
                await self.store.delete_token(_digest(token))
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
    ) -> dict[str, Any]:
        """Mint an access/refresh pair and return the RFC 6749 token response."""
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


def _error_response(error: str, description: str, status_code: int = 400) -> Response:
    """Build an RFC 6749 section 5.2 error response."""
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _redirect_error(redirect_uri: str, error: str, description: str, state: str | None) -> Response:
    """Send the error back to the client, per RFC 6749 section 4.1.2.1."""
    params = {"error": error, "error_description": description}
    if state is not None:
        params["state"] = state
    joiner = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{joiner}{urlencode(params)}", status_code=HTTP_302_FOUND
    )


def _redirect_uri_is_allowed(uri: str) -> bool:
    """Whether a client may be redirected here at all.

    OAuth 2.1 requires https, with an exception for the loopback interface that
    a native client needs to receive its callback.
    """
    parsed = urlparse(uri)
    if parsed.scheme == "https":
        return True
    if parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS:
        return True
    # A private-use scheme (`com.example.app:/callback`) is how a mobile client
    # receives its callback; it never travels a network.
    return bool(parsed.scheme) and parsed.scheme not in {"http", "https"}


def _verify_pkce(verifier: str, challenge: str) -> bool:
    """Whether `verifier` hashes to `challenge` under S256."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, challenge)


def register_authorization_server(
    app: Any,
    server: MCPAuthorizationServer,
    prefix: str = "",
    exclude_middleware: Sequence[str] | None = None,
) -> None:
    """Mount `server`'s OAuth endpoints on `app`.

    Registers the RFC 8414 metadata document, `/authorize`, `/token`, and - when
    dynamic registration is on - `/register`. `prefix` mounts them under a path
    segment; the metadata document advertises whatever the issuer says, so the
    prefix and the issuer must agree.
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


async def _handle_authorize(server: MCPAuthorizationServer, request: Request) -> Response:
    """Authenticate the user and hand back a single-use code.

    Errors before the redirect target is trusted are shown here rather than
    redirected: sending them onward would make this server an open redirector.
    """
    params = request.query_params
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    state = params.get("state")

    if not client_id or not redirect_uri:
        return _error_response("invalid_request", "client_id and redirect_uri are required")
    client = await server.store.get_client(client_id)
    if client is None:
        return _error_response("invalid_client", "unknown client_id")
    # Exact match against what the client registered. A prefix or wildcard match
    # is what lets an attacker redirect the code to a path it controls.
    if redirect_uri not in client.redirect_uris:
        return _error_response("invalid_request", "redirect_uri is not registered for this client")

    # From here the redirect target is trusted, so failures travel back to it.
    if params.get("response_type") != "code":
        return _redirect_error(
            redirect_uri, "unsupported_response_type", "only response_type=code is supported", state
        )
    challenge = params.get("code_challenge")
    method = params.get("code_challenge_method")
    if not challenge:
        return _redirect_error(
            redirect_uri, "invalid_request", "code_challenge is required (PKCE)", state
        )
    if method != _PKCE_S256:
        return _redirect_error(
            redirect_uri,
            "invalid_request",
            f"code_challenge_method must be {_PKCE_S256}",
            state,
        )

    requested = frozenset((params.get("scope") or "").split())
    allowed = frozenset(server.scopes_supported)
    if requested - allowed:
        return _redirect_error(
            redirect_uri,
            "invalid_scope",
            f"unsupported scope: {' '.join(sorted(requested - allowed))}",
            state,
        )

    outcome = server.authenticate(request)
    if hasattr(outcome, "__await__"):
        outcome = await outcome  # type: ignore[misc]
    if isinstance(outcome, Response):
        # The application is taking the browser somewhere first - a login form, a
        # consent screen - and will drive this endpoint again afterwards.
        return outcome
    if outcome is None:
        return _redirect_error(redirect_uri, "access_denied", "the request was not approved", state)

    code = secrets.token_urlsafe(_CREDENTIAL_ENTROPY_BYTES)
    await server.store.save_code(
        _digest(code),
        AuthorizationCode(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            subject=outcome.subject,
            scopes=requested & outcome.scopes if requested else outcome.scopes,
            resource=params.get("resource"),
            expires_at=_now() + _CODE_TTL_SECONDS,
            claims=dict(outcome.claims),
        ),
    )
    granted = {"code": code}
    if state is not None:
        granted["state"] = state
    joiner = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{joiner}{urlencode(granted)}", status_code=HTTP_302_FOUND
    )


async def _handle_token(server: MCPAuthorizationServer, request: Request) -> Response:
    """Exchange a code, or a refresh token, for an access token."""
    form = await request.form()
    grant_type = form.get("grant_type")
    if grant_type == "authorization_code":
        return await _grant_authorization_code(server, form)
    if grant_type == "refresh_token":
        return await _grant_refresh_token(server, form)
    return _error_response("unsupported_grant_type", f"unsupported grant_type: {grant_type!r}")


async def _authenticate_client(
    server: MCPAuthorizationServer, form: Any
) -> tuple[OAuthClient | None, Response | None]:
    """Resolve the client making a token request, verifying a secret if it has one."""
    client_id = form.get("client_id")
    if not client_id:
        return None, _error_response("invalid_client", "client_id is required")
    client = await server.store.get_client(client_id)
    if client is None:
        return None, _error_response("invalid_client", "unknown client_id", status_code=401)
    if client.client_secret_digest is not None:
        secret = form.get("client_secret") or ""
        if not hmac.compare_digest(_digest(secret), client.client_secret_digest):
            return None, _error_response("invalid_client", "client authentication failed", 401)
    return client, None


async def _grant_authorization_code(server: MCPAuthorizationServer, form: Any) -> Response:
    """Redeem a code: single-use, PKCE-verified, and bound to its own request."""
    client, failure = await _authenticate_client(server, form)
    if failure is not None:
        return failure
    assert client is not None

    code = form.get("code")
    verifier = form.get("code_verifier")
    if not code or not verifier:
        return _error_response("invalid_request", "code and code_verifier are required")

    # Taken, not read: a replay finds nothing even if the checks below fail.
    record = await server.store.take_code(_digest(code))
    if record is None:
        return _error_response("invalid_grant", "the code is unknown or has already been used")
    if record.expires_at <= _now():
        return _error_response("invalid_grant", "the code has expired")
    if record.client_id != client.client_id:
        return _error_response("invalid_grant", "the code was issued to another client")
    if record.redirect_uri != form.get("redirect_uri"):
        return _error_response("invalid_grant", "redirect_uri does not match the authorization")
    if not _verify_pkce(verifier, record.code_challenge):
        return _error_response("invalid_grant", "code_verifier does not match the challenge")

    issued = await server._issue(
        client.client_id, record.subject, record.scopes, record.resource, record.claims
    )
    return JSONResponse(issued, headers={"Cache-Control": "no-store"})


async def _grant_refresh_token(server: MCPAuthorizationServer, form: Any) -> Response:
    """Exchange a refresh token, rotating it so the old one stops working."""
    client, failure = await _authenticate_client(server, form)
    if failure is not None:
        return failure
    assert client is not None

    refresh = form.get("refresh_token")
    if not refresh:
        return _error_response("invalid_request", "refresh_token is required")
    taken = await server.store.take_refresh(_digest(refresh))
    if taken is None:
        return _error_response("invalid_grant", "the refresh token is unknown or has been used")
    _old_digest, record = taken
    if record.client_id != client.client_id:
        return _error_response("invalid_grant", "the token was issued to another client")

    issued = await server._issue(
        client.client_id, record.subject, record.scopes, record.resource, record.claims
    )
    return JSONResponse(issued, headers={"Cache-Control": "no-store"})


async def _handle_register(server: MCPAuthorizationServer, request: Request) -> Response:
    """Register a client on the spot (RFC 7591), so no credential is pre-arranged."""
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return _error_response("invalid_client_metadata", "the body must be a JSON object")

    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        return _error_response("invalid_redirect_uri", "redirect_uris must be a non-empty list")
    for uri in uris:
        if not _redirect_uri_is_allowed(uri):
            return _error_response(
                "invalid_redirect_uri",
                f"{uri!r} must be https, a loopback http address, or a private-use scheme",
            )

    requested = frozenset((body.get("scope") or "").split())
    allowed = frozenset(server.scopes_supported)
    if requested - allowed:
        return _error_response(
            "invalid_client_metadata",
            f"unsupported scope: {' '.join(sorted(requested - allowed))}",
        )

    client_id = secrets.token_urlsafe(_CREDENTIAL_ENTROPY_BYTES)
    # A client that cannot keep a secret must not be given one to leak; PKCE is
    # what proves it. `token_endpoint_auth_method: "none"` is how it says so.
    wants_secret = body.get("token_endpoint_auth_method", "none") != "none"
    secret = secrets.token_urlsafe(_CREDENTIAL_ENTROPY_BYTES) if wants_secret else None
    await server.store.save_client(
        OAuthClient(
            client_id=client_id,
            redirect_uris=tuple(uris),
            client_secret_digest=_digest(secret) if secret is not None else None,
            client_name=body.get("client_name"),
            scopes=requested or allowed,
        )
    )
    registered: dict[str, Any] = {
        "client_id": client_id,
        "redirect_uris": list(uris),
        "token_endpoint_auth_method": "client_secret_post" if secret else "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    if body.get("client_name"):
        registered["client_name"] = body["client_name"]
    if secret is not None:
        # The only time the secret is ever readable; only its digest is kept.
        registered["client_secret"] = secret
    return JSONResponse(registered, status_code=201, headers={"Cache-Control": "no-store"})
