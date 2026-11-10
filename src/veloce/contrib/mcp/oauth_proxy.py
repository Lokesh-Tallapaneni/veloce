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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlencode

from veloce._protocol_constants import HTTP_METHOD_GET, HTTP_METHOD_POST
from veloce.contrib.mcp.authorization import (
    _PKCE_S256,
    AUTHORIZATION_SERVER_METADATA_PATH,
    _digest,
    _error_response,
    _now,
    _redirect_error,
    _redirect_uri_is_allowed,
    _verify_pkce,
)
from veloce.http.response import JSONResponse, RedirectResponse, Response
from veloce.security.jwt import encode_jwt
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

    def metadata(self) -> dict[str, Any]:
        """Build the RFC 8414 authorization server metadata document."""
        document: dict[str, Any] = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer.rstrip('/')}/authorize",
            "token_endpoint": f"{self.issuer.rstrip('/')}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": [_PKCE_S256],
            "token_endpoint_auth_methods_supported": ["none"],
        }
        if self.scopes_supported:
            document["scopes_supported"] = list(self.scopes_supported)
        if self.allow_dynamic_registration:
            document["registration_endpoint"] = f"{self.issuer.rstrip('/')}/register"
        return document


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

    body = await _exchange_at_upstream(
        proxy,
        {
            "grant_type": "authorization_code",
            "code": upstream_code,
            "redirect_uri": proxy.callback_url,
            "client_id": proxy.upstream_client_id,
            "client_secret": proxy.upstream_client_secret,
        },
    )
    if body is None:
        return _redirect_error(
            pending.client_redirect_uri,
            "server_error",
            "the upstream rejected the code exchange",
            pending.client_state,
        )

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


def _mint_jwt(proxy: UpstreamOAuthProxy, jti: str, tokens: UpstreamTokenSet) -> str:
    """Sign a client-facing access token standing in for the upstream's own."""
    claims: dict[str, Any] = {
        "iss": proxy.issuer,
        "jti": jti,
        "iat": _now(),
        "exp": _now() + proxy.access_token_ttl,
    }
    subject = tokens.claims.get("sub")
    if subject is not None:
        claims["sub"] = subject
    scope = " ".join(proxy.scopes_supported)
    if scope:
        claims["scope"] = scope
    return encode_jwt(claims, proxy.signing_key, alg="HS256")


def _token_response(proxy: UpstreamOAuthProxy, jti: str, tokens: UpstreamTokenSet) -> Response:
    """Build the RFC 6749 Sec. 5.1 response carrying the minted JWT.

    `refresh_token` is the `jti` itself: the only thing a refresh needs to
    find which stored upstream token set to re-exchange.
    """
    return JSONResponse(
        {
            "access_token": _mint_jwt(proxy, jti, tokens),
            "token_type": "Bearer",
            "expires_in": int(proxy.access_token_ttl),
            "refresh_token": jti,
        },
        headers={"Cache-Control": "no-store"},
    )


async def _exchange_at_upstream(
    proxy: UpstreamOAuthProxy, data: dict[str, str]
) -> dict[str, Any] | None:
    """POST a grant to the upstream token endpoint; `None` on any failure."""
    httpx2 = _httpx2()
    try:
        async with httpx2.AsyncClient(timeout=httpx2.Timeout(10.0)) as client:
            response = await client.post(proxy.upstream_token_endpoint, data=data)
    except httpx2.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    body: dict[str, Any] = response.json()
    return body


async def _grant_authorization_code(proxy: UpstreamOAuthProxy, form: Any) -> Response:
    """Redeem a proxy-issued code: single-use, PKCE-verified against the client."""
    code = form.get("code")
    verifier = form.get("code_verifier")
    if not code or not verifier:
        return _error_response("invalid_request", "code and code_verifier are required")

    # Taken, not read: a replay finds nothing even if the checks below fail.
    record = await proxy.store.take_code(_digest(code))
    if record is None:
        return _error_response("invalid_grant", "the code is unknown or has already been used")
    if record.expires_at <= _now():
        return _error_response("invalid_grant", "the code has expired")
    if record.client_redirect_uri != form.get("redirect_uri"):
        return _error_response("invalid_grant", "redirect_uri does not match the authorization")
    if not _verify_pkce(verifier, record.client_code_challenge):
        return _error_response("invalid_grant", "code_verifier does not match the challenge")

    tokens = await proxy.store.get_upstream_tokens(record.jti)
    if tokens is None:
        return _error_response("invalid_grant", "the upstream session behind this code is gone")
    return _token_response(proxy, record.jti, tokens)


async def _grant_refresh_token(proxy: UpstreamOAuthProxy, form: Any) -> Response:
    """Re-run the upstream exchange with its refresh token and re-mint a JWT."""
    jti = form.get("refresh_token")
    if not jti:
        return _error_response("invalid_request", "refresh_token is required")
    tokens = await proxy.store.get_upstream_tokens(jti)
    if tokens is None:
        return _error_response("invalid_grant", "the refresh token is unknown or has expired")
    if tokens.refresh_token is None:
        return _error_response("invalid_grant", "the upstream issued no refresh token")

    body = await _exchange_at_upstream(
        proxy,
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": proxy.upstream_client_id,
            "client_secret": proxy.upstream_client_secret,
        },
    )
    if body is None:
        return _error_response("invalid_grant", "the upstream rejected the refresh")

    refreshed = UpstreamTokenSet(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", tokens.refresh_token),
        expires_at=_now() + float(body.get("expires_in", proxy.access_token_ttl)),
        claims=body,
    )
    await proxy.store.save_upstream_tokens(jti, refreshed)
    return _token_response(proxy, jti, refreshed)


async def _handle_token(proxy: UpstreamOAuthProxy, request: Request) -> Response:
    """Exchange a proxy-issued code, or refresh one, for a locally-minted JWT."""
    form = await request.form()
    grant_type = form.get("grant_type")
    if grant_type == "authorization_code":
        return await _grant_authorization_code(proxy, form)
    if grant_type == "refresh_token":
        return await _grant_refresh_token(proxy, form)
    return _error_response("unsupported_grant_type", f"unsupported grant_type: {grant_type!r}")


async def _handle_register(proxy: UpstreamOAuthProxy, request: Request) -> Response:
    """Fake RFC 7591 registration: hand back this proxy's one upstream credential.

    There is no per-client registry - every registrant receives the same
    `client_id`, because this proxy itself registered only once with the
    upstream. PKCE, not `client_id`, is what distinguishes one flow from
    another at `/authorize` and `/token`.
    """
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

    registered: dict[str, Any] = {
        "client_id": proxy.upstream_client_id,
        "redirect_uris": list(uris),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    if proxy.scopes_supported:
        registered["scope"] = " ".join(proxy.scopes_supported)
    return JSONResponse(registered, status_code=201, headers={"Cache-Control": "no-store"})


def register_oauth_proxy(
    app: Any,
    proxy: UpstreamOAuthProxy,
    *,
    prefix: str = "",
    exclude_middleware: Sequence[str] | None = None,
) -> None:
    """Mount `proxy`'s endpoints on `app`.

    Registers the RFC 8414 metadata document, `/authorize`, the proxy's own
    fixed upstream callback, `/token`, and - when dynamic registration is on -
    `/register`. The metadata advertises whatever the issuer says, so
    `prefix` and the issuer must agree.
    """
    base = prefix.rstrip("/")

    async def metadata(request: Request) -> Response:
        return JSONResponse(proxy.metadata(), headers={"Cache-Control": "no-store"})

    async def authorize(request: Request) -> Response:
        return await _handle_authorize(proxy, request)

    async def callback(request: Request) -> Response:
        return await _handle_upstream_callback(proxy, request)

    async def token(request: Request) -> Response:
        return await _handle_token(proxy, request)

    async def register(request: Request) -> Response:
        return await _handle_register(proxy, request)

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
        f"{base}{proxy.redirect_path}",
        callback,
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
    if proxy.allow_dynamic_registration:
        app.add_route(
            f"{base}/register",
            register,
            methods=[HTTP_METHOD_POST],
            include_in_schema=False,
            exclude_middleware=exclude_middleware,
        )
