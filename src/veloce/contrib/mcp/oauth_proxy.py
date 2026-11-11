"""Upstream OAuth proxy, fronting a real OAuth/OIDC identity provider.

`/authorize` redirects the browser to the upstream using this proxy's own
fixed callback; the callback exchanges the upstream's code, stores its token
set, and mints a locally-signed JWT a `/token` leg hands back to the client.
`MCPAuth` verifies that JWT like any other bearer token - no new
resource-server machinery.
"""

from __future__ import annotations

import base64
import secrets
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlencode

import veloce._json as _json
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
#: How long an abandoned `/authorize` flow stays redeemable at the callback.
_PENDING_AUTHORIZATION_TTL_SECONDS = 600.0
#: How long a stored upstream token set may sit untouched - by a redemption
#: or a refresh - before it is swept as abandoned. Deliberately generous:
#: this bounds growth from flows nobody ever came back for, not normal use.
_UPSTREAM_TOKEN_SET_MAX_IDLE_SECONDS = 30 * 24 * 3600.0


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


def _decode_unverified_jwt_payload(token: str) -> dict[str, Any]:
    """Decode a compact JWS payload without verifying its signature.

    Only ever called on an `id_token` the upstream itself returned over the
    authenticated token-endpoint exchange, never on anything a client
    presents to this proxy - that back-channel origin is what makes
    skipping signature verification acceptable here.
    """
    try:
        _header, payload_b64, _sig = token.split(".", 2)
    except ValueError:
        return {}
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        payload = _json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_subject(body: dict[str, Any]) -> str | None:
    """Find the upstream subject in a token response: `id_token` first."""
    id_token = body.get("id_token")
    if isinstance(id_token, str):
        sub = _decode_unverified_jwt_payload(id_token).get("sub")
        if isinstance(sub, str):
            return sub
    sub = body.get("sub")
    return sub if isinstance(sub, str) else None


@dataclass(slots=True)
class PendingAuthorization:
    """One in-flight `/authorize` request, correlated to the upstream's callback."""

    client_redirect_uri: str
    client_code_challenge: str
    client_state: str | None = None
    resource: str | None = None
    expires_at: float = field(default_factory=lambda: _now() + _PENDING_AUTHORIZATION_TTL_SECONDS)


@dataclass(slots=True)
class UpstreamTokenSet:
    """The upstream's own tokens for one subject, kept to mint and refresh ours."""

    access_token: str
    refresh_token: str | None
    expires_at: float
    claims: dict[str, Any] = field(default_factory=dict)
    #: Taken from the upstream's `id_token`, where OIDC providers carry it -
    #: not from `claims`, which a refresh response can overwrite or omit.
    subject: str | None = None
    #: RFC 8707 resource this was requested for, carried into every JWT
    #: minted from it - including across a refresh, unlike `claims`.
    resource: str | None = None
    #: Stamped fresh on every save (initial exchange or refresh) - how the
    #: store tells an active session from one nobody ever came back to.
    last_active_at: float = field(default_factory=_now)


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

    async def save_refresh(self, refresh_digest: str, jti: str) -> None:
        """Record which `jti` a client-facing refresh token stands for."""

    async def get_refresh(self, refresh_digest: str) -> str | None:
        """Return the `jti` recorded under `refresh_digest`, without removing it."""

    async def take_refresh(self, refresh_digest: str) -> str | None:
        """Return and remove the `jti` recorded under `refresh_digest`."""


class InMemoryUpstreamOAuthProxyStore:
    """Single-process, dict-backed `UpstreamOAuthProxyStore` - lost on restart."""

    __slots__ = ("_codes", "_pending", "_refresh", "_tokens")

    def __init__(self) -> None:
        self._pending: dict[str, PendingAuthorization] = {}
        self._codes: dict[str, ProxyAuthorizationCode] = {}
        self._tokens: dict[str, UpstreamTokenSet] = {}
        self._refresh: dict[str, str] = {}

    async def save_pending(self, state: str, pending: PendingAuthorization) -> None:
        # Opportunistic sweep: an abandoned flow that never reaches the
        # callback would otherwise sit here forever.
        now = _now()
        expired = [key for key, value in self._pending.items() if value.expires_at <= now]
        for key in expired:
            del self._pending[key]
        self._pending[state] = pending

    async def take_pending(self, state: str) -> PendingAuthorization | None:
        return self._pending.pop(state, None)

    async def save_code(self, code_digest: str, code: ProxyAuthorizationCode) -> None:
        # Opportunistic sweep: a code the client never redeems would
        # otherwise sit here forever, same reasoning as `save_pending`.
        now = _now()
        expired = [key for key, value in self._codes.items() if value.expires_at <= now]
        for key in expired:
            del self._codes[key]
        self._codes[code_digest] = code

    async def take_code(self, code_digest: str) -> ProxyAuthorizationCode | None:
        return self._codes.pop(code_digest, None)

    async def save_upstream_tokens(self, jti: str, tokens: UpstreamTokenSet) -> None:
        # Opportunistic sweep: a session nobody has redeemed or refreshed in
        # `_UPSTREAM_TOKEN_SET_MAX_IDLE_SECONDS` is treated as abandoned -
        # `delete_upstream_tokens` exists but nothing else ever calls it.
        now = _now()
        idle = [
            key
            for key, value in self._tokens.items()
            if now - value.last_active_at > _UPSTREAM_TOKEN_SET_MAX_IDLE_SECONDS
        ]
        for key in idle:
            del self._tokens[key]
        self._tokens[jti] = tokens

    async def get_upstream_tokens(self, jti: str) -> UpstreamTokenSet | None:
        return self._tokens.get(jti)

    async def delete_upstream_tokens(self, jti: str) -> None:
        self._tokens.pop(jti, None)

    async def save_refresh(self, refresh_digest: str, jti: str) -> None:
        self._refresh[refresh_digest] = jti

    async def get_refresh(self, refresh_digest: str) -> str | None:
        return self._refresh.get(refresh_digest)

    async def take_refresh(self, refresh_digest: str) -> str | None:
        return self._refresh.pop(refresh_digest, None)


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
            allowed_redirect_uris=["https://my-mcp-client.example/callback"],
        )
        register_oauth_proxy(app, proxy)

    `allowed_redirect_uris` defaults to empty, which accepts nothing: with
    no per-client registry, any redirect_uri this proxy would otherwise
    accept is where a stolen authorization code ends up, so every one an
    MCP client will use must be listed explicitly.
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
    #: Exact client redirect_uris this proxy will ever redirect a browser
    #: to or redirect an error to. Empty means none - fail closed, since
    #: there is no per-client registry to check membership against instead.
    allowed_redirect_uris: Iterable[str] = ()
    #: Scopes the minted JWT carries and advertises to MCP clients.
    scopes_supported: Iterable[str] = ()
    #: Scopes requested from the upstream at `/authorize` - a provider's own
    #: vocabulary (e.g. `openid email profile`), not `scopes_supported`,
    #: which the upstream has never heard of and may refuse as unknown.
    upstream_scopes: Iterable[str] = ()
    access_token_ttl: float = 3600.0
    allow_dynamic_registration: bool = True
    store: UpstreamOAuthProxyStore = field(default_factory=InMemoryUpstreamOAuthProxyStore)
    #: Lazily created, reused across every upstream call - never set by hand.
    _http_client: Any = field(default=None, init=False, repr=False, compare=False)

    @property
    def callback_url(self) -> str:
        """This proxy's fixed redirect_uri, as presented to the upstream."""
        return self.issuer.rstrip("/") + self.redirect_path

    async def aclose(self) -> None:
        """Close the persistent upstream HTTP client, if one was ever created.

        Call this from the app's own shutdown hook; the proxy has no
        lifecycle of its own to do it automatically.
        """
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

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
    allowed = frozenset(proxy.allowed_redirect_uris)
    if (
        not redirect_uri
        or not _redirect_uri_is_allowed(redirect_uri)
        or redirect_uri not in allowed
    ):
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
            resource=params.get("resource"),
        ),
    )
    upstream_params = {
        "response_type": "code",
        "client_id": proxy.upstream_client_id,
        "redirect_uri": proxy.callback_url,
        "state": upstream_state,
    }
    scope = " ".join(proxy.upstream_scopes)
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
    if pending is None or pending.expires_at <= _now():
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
            subject=_extract_subject(body),
            resource=pending.resource,
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


def _mint_jwt(
    proxy: UpstreamOAuthProxy, jti: str, tokens: UpstreamTokenSet, resource: str | None
) -> str:
    """Sign a client-facing access token standing in for the upstream's own."""
    claims: dict[str, Any] = {
        "iss": proxy.issuer,
        "jti": jti,
        "iat": _now(),
        "exp": _now() + proxy.access_token_ttl,
    }
    if tokens.subject is not None:
        claims["sub"] = tokens.subject
    if resource is not None:
        # RFC 8707: binds the token to the resource it was requested for, so
        # a token minted for one MCP server is identifiable as such.
        claims["aud"] = resource
    scope = " ".join(proxy.scopes_supported)
    if scope:
        claims["scope"] = scope
    return encode_jwt(claims, proxy.signing_key, alg="HS256")


async def _token_response(
    proxy: UpstreamOAuthProxy, jti: str, tokens: UpstreamTokenSet, resource: str | None
) -> Response:
    """Build the RFC 6749 Sec. 5.1 response carrying the minted JWT.

    Issues a fresh, random `refresh_token` of its own rather than handing
    back `jti` directly - `jti` is a plaintext claim inside the JWT above,
    so using it as the refresh credential too would let anyone who merely
    sees an access token mint new ones forever.
    """
    refresh_token = secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    await proxy.store.save_refresh(_digest(refresh_token), jti)
    return JSONResponse(
        {
            "access_token": _mint_jwt(proxy, jti, tokens, resource),
            "token_type": "Bearer",
            "expires_in": int(proxy.access_token_ttl),
            "refresh_token": refresh_token,
        },
        headers={"Cache-Control": "no-store"},
    )


def _upstream_client(proxy: UpstreamOAuthProxy) -> Any:
    """Return `proxy`'s persistent `httpx2` client, creating it on first use.

    Every call targets the same `upstream_token_endpoint`, so reusing one
    pooled client avoids a fresh TCP/TLS handshake per exchange.
    """
    if proxy._http_client is None:
        httpx2 = _httpx2()
        proxy._http_client = httpx2.AsyncClient(timeout=httpx2.Timeout(10.0))
    return proxy._http_client


async def _exchange_at_upstream(
    proxy: UpstreamOAuthProxy, data: dict[str, str]
) -> dict[str, Any] | None:
    """POST a grant to the upstream token endpoint; `None` on any failure."""
    httpx2 = _httpx2()
    client = _upstream_client(proxy)
    try:
        response = await client.post(proxy.upstream_token_endpoint, data=data)
    except httpx2.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
        return None
    if "expires_in" in body:
        expires_in = body["expires_in"]
        # A `null` expires_in is also rejected here: `body.get(..., default)`
        # downstream only falls back on a MISSING key, not a present-but-null
        # one, so letting None through would still crash on `float(None)`.
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
            return None
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
    return await _token_response(proxy, record.jti, tokens, tokens.resource)


async def _grant_refresh_token(proxy: UpstreamOAuthProxy, form: Any) -> Response:
    """Re-run the upstream exchange with its refresh token and re-mint a JWT.

    The presented refresh token is only invalidated once the upstream
    exchange actually succeeds - peeked first, taken last - so a transient
    upstream failure (a timeout, a 5xx) does not burn the client's only
    refresh credential and force it back through `/authorize`.
    """
    presented = form.get("refresh_token")
    if not presented:
        return _error_response("invalid_request", "refresh_token is required")
    digest = _digest(presented)
    jti = await proxy.store.get_refresh(digest)
    if jti is None:
        return _error_response("invalid_grant", "the refresh token is unknown or has expired")
    tokens = await proxy.store.get_upstream_tokens(jti)
    if tokens is None:
        return _error_response("invalid_grant", "the upstream session behind this token is gone")
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
    await proxy.store.take_refresh(digest)

    refreshed = UpstreamTokenSet(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", tokens.refresh_token),
        expires_at=_now() + float(body.get("expires_in", proxy.access_token_ttl)),
        claims=body,
        subject=_extract_subject(body) or tokens.subject,
        resource=tokens.resource,
    )
    await proxy.store.save_upstream_tokens(jti, refreshed)
    return await _token_response(proxy, jti, refreshed, refreshed.resource)


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
    allowed = frozenset(proxy.allowed_redirect_uris)
    for uri in uris:
        if not _redirect_uri_is_allowed(uri) or uri not in allowed:
            return _error_response(
                "invalid_redirect_uri",
                f"{uri!r} is not in this proxy's allowed_redirect_uris",
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
