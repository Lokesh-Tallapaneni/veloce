"""Authorization endpoint handlers — authorize, token, register and the grants."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlparse

from veloce._internal import _b64encode
from veloce.contrib.mcp.authorization._records import (
    _CODE_TTL_SECONDS,
    _CREDENTIAL_ENTROPY_BYTES,
    _LOOPBACK_HOSTS,
    _PKCE_S256,
    AuthorizationCode,
    OAuthClient,
    _digest,
    _now,
)
from veloce.http.response import JSONResponse, RedirectResponse, Response
from veloce.safe import constant_time_compare
from veloce.status import HTTP_302_FOUND

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.authorization._server import MCPAuthorizationServer
    from veloce.http.request import Request

_logger = logging.getLogger("veloce.contrib.mcp.authorization")


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
    # A private-use scheme (`com.example.app:/callback`) never travels a network.
    return bool(parsed.scheme) and parsed.scheme not in {"http", "https"}


def _verify_pkce(verifier: str, challenge: str) -> bool:
    """Whether `verifier` hashes to `challenge` under S256."""
    # RFC 7636 Sec. 4.1 confines a verifier to unreserved ASCII, and it arrives
    # unauthenticated, so a non-ASCII one must refuse rather than raise.
    try:
        verifier_bytes = verifier.encode("ascii")
    except UnicodeEncodeError:
        return False
    digest = hashlib.sha256(verifier_bytes).digest()
    # RFC 7636 Sec. 4.2: BASE64URL-ENCODE(SHA256(verifier)), padding stripped.
    computed = _b64encode(digest)
    # `hmac.compare_digest` raises on a non-ASCII `str`; this encodes first.
    return constant_time_compare(computed, challenge)


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
    # Exact: a prefix or wildcard match lets an attacker redirect the code.
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

    answer = server.authenticate(request)
    outcome = await answer if isinstance(answer, Awaitable) else answer
    if isinstance(outcome, Response):
        # A login form or consent screen; it drives this endpoint again after.
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


SUPPORTED_GRANT_TYPES: tuple[str, ...] = ("authorization_code", "refresh_token")
"""Grant types the server accepts; a registration naming anything else is refused, not silently narrowed."""


def _refuse_unregistered_grant(client: OAuthClient, grant_type: str) -> Response | None:
    """Refuse a grant the client is not registered for, per RFC 6749 Sec. 5.2.

    The registration is the contract, and this is where it binds.
    """
    if grant_type in client.grant_types:
        return None
    return _error_response(
        "unauthorized_client",
        f"this client is not registered for the {grant_type!r} grant type",
    )


async def _grant_authorization_code(server: MCPAuthorizationServer, form: Any) -> Response:
    """Redeem a code: single-use, PKCE-verified, and bound to its own request."""
    client, failure = await _authenticate_client(server, form)
    if failure is not None:
        return failure
    assert client is not None
    refused = _refuse_unregistered_grant(client, "authorization_code")
    if refused is not None:
        return refused

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


async def _spent_family(store: Any, refresh_digest: str) -> str | None:
    """Ask the store which family an already-spent refresh token belonged to.

    Optional on the `AuthorizationStore` protocol: a store that cannot answer
    leaves a replay with the plain `invalid_grant`, so requiring the method
    would break every custom store on upgrade.
    """
    query = getattr(store, "family_of_spent_refresh", None)
    if query is None:
        return None
    family = await query(refresh_digest)
    return family if isinstance(family, str) else None


async def _revoke_family(store: Any, family_id: str) -> None:
    """Drop every token descended from one authorization, where supported."""
    revoke = getattr(store, "revoke_family", None)
    if revoke is not None:
        await revoke(family_id)


async def _grant_refresh_token(server: MCPAuthorizationServer, form: Any) -> Response:
    """Exchange a refresh token, rotating it so the old one stops working."""
    client, failure = await _authenticate_client(server, form)
    if failure is not None:
        return failure
    assert client is not None
    refused = _refuse_unregistered_grant(client, "refresh_token")
    if refused is not None:
        return refused

    refresh = form.get("refresh_token")
    if not refresh:
        return _error_response("invalid_request", "refresh_token is required")
    presented = _digest(refresh)
    taken = await server.store.take_refresh(presented)
    if taken is None:
        # An already-spent token means the spender kept a copy, or someone else
        # did. OAuth 2.1 Sec. 4.14.2 treats that as compromise of the whole
        # chain, so refusing only this request would leave the pair usable.
        family = await _spent_family(server.store, presented)
        if family is not None:
            await _revoke_family(server.store, family)
            _logger.warning(
                "MCP authorization: refresh token reuse detected for client %r; "
                "revoked the token family it belonged to.",
                client.client_id,
            )
            return _error_response(
                "invalid_grant",
                "this refresh token was already used; the session has been revoked",
            )
        return _error_response("invalid_grant", "the refresh token is unknown or has been used")
    _old_digest, record = taken
    if record.client_id != client.client_id:
        # Presented by another client: the same compromise signal.
        await _revoke_family(server.store, record.family_id)
        return _error_response("invalid_grant", "the token was issued to another client")

    issued = await server._issue(
        client.client_id,
        record.subject,
        record.scopes,
        record.resource,
        record.claims,
        family_id=record.family_id,
    )
    return JSONResponse(issued, headers={"Cache-Control": "no-store"})


async def _handle_register(server: MCPAuthorizationServer, request: Request) -> Response:
    """Register a client on the spot (RFC 7591), so no credential is pre-arranged."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an undecodable body is treated as absent
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

    # RFC 7591 Sec. 2. Omitted keeps the historical default: the spec's narrower
    # `["authorization_code"]` would silently stop refresh for existing clients.
    grants = body.get("grant_types", list(SUPPORTED_GRANT_TYPES))
    if not isinstance(grants, list) or not all(isinstance(g, str) for g in grants):
        return _error_response("invalid_client_metadata", "grant_types must be a list of strings")
    unsupported = [g for g in grants if g not in SUPPORTED_GRANT_TYPES]
    if unsupported:
        return _error_response(
            "invalid_client_metadata",
            f"unsupported grant_types: {' '.join(sorted(unsupported))}",
        )
    if "authorization_code" not in grants:
        return _error_response(
            "invalid_client_metadata",
            "grant_types must include 'authorization_code'; it is the only way to obtain a token",
        )

    requested = frozenset((body.get("scope") or "").split())
    allowed = frozenset(server.scopes_supported)
    if requested - allowed:
        return _error_response(
            "invalid_client_metadata",
            f"unsupported scope: {' '.join(sorted(requested - allowed))}",
        )

    client_id = secrets.token_urlsafe(_CREDENTIAL_ENTROPY_BYTES)
    # A client that cannot keep a secret is given none; PKCE proves it instead.
    wants_secret = body.get("token_endpoint_auth_method", "none") != "none"
    secret = secrets.token_urlsafe(_CREDENTIAL_ENTROPY_BYTES) if wants_secret else None
    stored = OAuthClient(
        client_id=client_id,
        redirect_uris=tuple(uris),
        client_secret_digest=_digest(secret) if secret is not None else None,
        client_name=body.get("client_name"),
        scopes=requested or allowed,
        grant_types=tuple(grants),
    )
    await server.store.save_client(stored)
    registered: dict[str, Any] = {
        "client_id": client_id,
        "redirect_uris": list(uris),
        "token_endpoint_auth_method": "client_secret_post" if secret else "none",
        # What was stored: a client told it holds a grant it lacks sees only a bug.
        "grant_types": list(grants),
        "response_types": ["code"],
    }
    # The response describes the registration, not the request that asked.
    if stored.client_name:
        registered["client_name"] = stored.client_name
    if secret is not None:
        # The only time the secret is ever readable; only its digest is kept.
        registered["client_secret"] = secret
    return JSONResponse(registered, status_code=201, headers={"Cache-Control": "no-store"})
