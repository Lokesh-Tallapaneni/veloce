"""The OAuth 2.1 authorization server for MCP clients.

Issues the tokens `MCPAuth` validates, for a deployment with no identity provider
to delegate to. Most of what matters here is what it *refuses*: a code that is
replayed, a verifier that does not match its challenge, a redirect to somewhere
the client never registered.

Tokens are opaque — entropy from `secrets`, stored as a digest — so there is no
signing key and a leaked store yields nothing usable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse

import pytest

from veloce import AsyncTestClient, Principal, RedirectResponse, Veloce
from veloce.contrib.mcp import (
    AuthorizationStore,
    InMemoryAuthorizationStore,
    MCPAuthorizationServer,
    OAuthClient,
    register_authorization_server,
)
from veloce.contrib.mcp.authorization import (
    AuthorizationCode,
    _digest,
    _now,
    _verify_pkce,
)

ISSUER = "https://api.example.com"
REDIRECT = "http://127.0.0.1:9876/callback"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def _server(**kwargs) -> MCPAuthorizationServer:
    kwargs.setdefault("issuer", ISSUER)
    kwargs.setdefault(
        "authenticate", lambda request: Principal(subject="user-42", scopes={"mcp:tools"})
    )
    kwargs.setdefault("scopes_supported", ["mcp:tools"])
    return MCPAuthorizationServer(**kwargs)


def _app(server: MCPAuthorizationServer) -> Veloce:
    app = Veloce(title="Secured", version="1.0.0", openapi_url=None)
    register_authorization_server(app, server)
    return app


async def _register(client, **body) -> dict:
    body.setdefault("redirect_uris", [REDIRECT])
    response = await client.post("/register", json=body)
    return json.loads(response.body)


def _authorize_query(client_id: str, challenge: str, **overrides) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
        "scope": "mcp:tools",
    }
    params.update({k: v for k, v in overrides.items() if v is not None})
    for key, value in overrides.items():
        if value is None:
            params.pop(key, None)
    return urllib.parse.urlencode(params)


def _code_from(location: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["code"][0]


def _error_from(location: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["error"][0]


# ── Discovery ────────────────────────────────────────────────────────


def test_the_metadata_names_the_endpoints_a_client_walks_to():
    metadata = _server().metadata()
    assert metadata["issuer"] == ISSUER
    assert metadata["authorization_endpoint"] == f"{ISSUER}/authorize"
    assert metadata["token_endpoint"] == f"{ISSUER}/token"
    assert metadata["response_types_supported"] == ["code"]


def test_only_s256_is_advertised():
    """A client reading this knows not to offer `plain`."""
    assert _server().metadata()["code_challenge_methods_supported"] == ["S256"]


def test_the_registration_endpoint_is_advertised_only_when_it_is_served():
    assert "registration_endpoint" in _server().metadata()
    assert "registration_endpoint" not in _server(allow_dynamic_registration=False).metadata()


async def test_the_metadata_is_served_at_the_well_known_path():
    async with AsyncTestClient(_app(_server())) as client:
        response = await client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        assert json.loads(response.body)["issuer"] == ISSUER
        assert response.headers["cache-control"] == "no-store"


# ── Construction refuses an unusable server ──────────────────────────


def test_a_cleartext_issuer_is_refused():
    """Every token would travel in the clear."""
    with pytest.raises(ValueError, match="must be https"):
        _server(issuer="http://api.example.com")


def test_a_loopback_issuer_is_allowed_for_development():
    assert _server(issuer="http://127.0.0.1:8000").issuer == "http://127.0.0.1:8000"


def test_an_empty_issuer_is_refused():
    with pytest.raises(ValueError, match="requires an issuer"):
        _server(issuer="")


@pytest.mark.parametrize("ttl", [0, -1])
def test_a_non_positive_token_lifetime_is_refused(ttl: int):
    with pytest.raises(ValueError, match="positive"):
        _server(access_token_ttl=ttl)


# ── Dynamic client registration ──────────────────────────────────────


async def test_registration_issues_a_client_id():
    async with AsyncTestClient(_app(_server())) as client:
        record = await _register(client, client_name="probe")
        assert record["client_id"]
        assert record["redirect_uris"] == [REDIRECT]
        assert record["client_name"] == "probe"


async def test_a_public_client_is_given_no_secret_to_leak():
    """PKCE is what proves it; a secret it cannot keep would be a liability."""
    async with AsyncTestClient(_app(_server())) as client:
        record = await _register(client)
        assert "client_secret" not in record
        assert record["token_endpoint_auth_method"] == "none"


async def test_a_confidential_client_is_given_a_secret_once():
    async with AsyncTestClient(_app(_server())) as client:
        record = await _register(client, token_endpoint_auth_method="client_secret_post")
        assert record["client_secret"]
        assert record["token_endpoint_auth_method"] == "client_secret_post"


async def test_only_the_secret_digest_is_kept():
    store = InMemoryAuthorizationStore()
    async with AsyncTestClient(_app(_server(store=store))) as client:
        record = await _register(client, token_endpoint_auth_method="client_secret_post")
        stored = await store.get_client(record["client_id"])
        assert stored is not None
        assert stored.client_secret_digest == _digest(record["client_secret"])
        assert record["client_secret"] not in json.dumps(stored.client_secret_digest)


@pytest.mark.parametrize("uris", [None, [], "not-a-list", [123]])
async def test_registration_requires_usable_redirect_uris(uris):
    async with AsyncTestClient(_app(_server())) as client:
        body = {} if uris is None else {"redirect_uris": uris}
        response = await client.post("/register", json=body)
        assert response.status_code == 400
        assert json.loads(response.body)["error"] == "invalid_redirect_uri"


async def test_a_cleartext_internet_redirect_is_refused():
    """A code sent back over http on the open internet is a code anyone can read."""
    async with AsyncTestClient(_app(_server())) as client:
        response = await client.post(
            "/register", json={"redirect_uris": ["http://example.com/callback"]}
        )
        assert response.status_code == 400


@pytest.mark.parametrize(
    "uri", ["https://example.com/cb", "http://localhost:1234/cb", "com.example.app:/cb"]
)
async def test_a_usable_redirect_is_accepted(uri: str):
    async with AsyncTestClient(_app(_server())) as client:
        response = await client.post("/register", json={"redirect_uris": [uri]})
        assert response.status_code == 201


async def test_registration_refuses_a_scope_the_server_does_not_issue():
    async with AsyncTestClient(_app(_server())) as client:
        response = await client.post(
            "/register", json={"redirect_uris": [REDIRECT], "scope": "root"}
        )
        assert response.status_code == 400


async def test_registration_refuses_a_body_that_is_not_an_object():
    async with AsyncTestClient(_app(_server())) as client:
        response = await client.post(
            "/register", content=b"[]", headers={"content-type": "application/json"}
        )
        assert response.status_code == 400


async def test_registration_is_not_served_when_it_is_off():
    async with AsyncTestClient(_app(_server(allow_dynamic_registration=False))) as client:
        response = await client.post("/register", json={"redirect_uris": [REDIRECT]})
        assert response.status_code == 404


# ── Authorize ────────────────────────────────────────────────────────


async def test_authorization_hands_back_a_code_and_the_state():
    async with AsyncTestClient(_app(_server())) as client:
        record = await _register(client)
        _verifier, challenge = _pkce()
        response = await client.get(
            f"/authorize?{_authorize_query(record['client_id'], challenge)}",
            follow_redirects=False,
        )
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith(REDIRECT)
        assert _code_from(location)
        assert urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["state"] == ["xyz"]


async def test_an_unknown_client_is_not_redirected_anywhere():
    """Redirecting an error to an unverified URI would make this an open redirector."""
    async with AsyncTestClient(_app(_server())) as client:
        _verifier, challenge = _pkce()
        response = await client.get(
            f"/authorize?{_authorize_query('never-registered', challenge)}",
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert json.loads(response.body)["error"] == "invalid_client"


async def test_an_unregistered_redirect_uri_is_not_redirected_to():
    async with AsyncTestClient(_app(_server())) as client:
        record = await _register(client)
        _verifier, challenge = _pkce()
        query = _authorize_query(
            record["client_id"], challenge, redirect_uri="https://attacker.example/cb"
        )
        response = await client.get(f"/authorize?{query}", follow_redirects=False)
        assert response.status_code == 400
        assert "location" not in response.headers


async def test_a_request_without_pkce_is_refused():
    async with AsyncTestClient(_app(_server())) as client:
        record = await _register(client)
        query = _authorize_query(record["client_id"], "unused", code_challenge=None)
        response = await client.get(f"/authorize?{query}", follow_redirects=False)
        assert _error_from(response.headers["location"]) == "invalid_request"


async def test_plain_pkce_is_refused():
    """`plain` puts the verifier on the wire, which defeats the exchange."""
    async with AsyncTestClient(_app(_server())) as client:
        record = await _register(client)
        _verifier, challenge = _pkce()
        query = _authorize_query(record["client_id"], challenge, code_challenge_method="plain")
        response = await client.get(f"/authorize?{query}", follow_redirects=False)
        assert _error_from(response.headers["location"]) == "invalid_request"


async def test_an_implicit_grant_is_refused():
    async with AsyncTestClient(_app(_server())) as client:
        record = await _register(client)
        _verifier, challenge = _pkce()
        query = _authorize_query(record["client_id"], challenge, response_type="token")
        response = await client.get(f"/authorize?{query}", follow_redirects=False)
        assert _error_from(response.headers["location"]) == "unsupported_response_type"


async def test_a_scope_the_server_does_not_issue_is_refused():
    async with AsyncTestClient(_app(_server())) as client:
        record = await _register(client)
        _verifier, challenge = _pkce()
        query = _authorize_query(record["client_id"], challenge, scope="root")
        response = await client.get(f"/authorize?{query}", follow_redirects=False)
        assert _error_from(response.headers["location"]) == "invalid_scope"


async def test_a_refused_login_is_reported_as_access_denied():
    async with AsyncTestClient(_app(_server(authenticate=lambda request: None))) as client:
        record = await _register(client)
        _verifier, challenge = _pkce()
        response = await client.get(
            f"/authorize?{_authorize_query(record['client_id'], challenge)}",
            follow_redirects=False,
        )
        assert _error_from(response.headers["location"]) == "access_denied"


async def test_the_application_may_send_the_browser_to_its_own_login():
    def to_login(request):
        return RedirectResponse("/login", status_code=302)

    async with AsyncTestClient(_app(_server(authenticate=to_login))) as client:
        record = await _register(client)
        _verifier, challenge = _pkce()
        response = await client.get(
            f"/authorize?{_authorize_query(record['client_id'], challenge)}",
            follow_redirects=False,
        )
        assert response.headers["location"] == "/login"


async def test_an_async_authenticator_is_awaited():
    async def authenticate(request):
        return Principal(subject="async-user", scopes={"mcp:tools"})

    server = _server(authenticate=authenticate)
    async with AsyncTestClient(_app(server)) as client:
        record = await _register(client)
        verifier, challenge = _pkce()
        response = await client.get(
            f"/authorize?{_authorize_query(record['client_id'], challenge)}",
            follow_redirects=False,
        )
        code = _code_from(response.headers["location"])
        issued = await _redeem(client, record["client_id"], code, verifier)
        principal = await server.verifier()(issued["access_token"])
        assert principal is not None
        assert principal.subject == "async-user"


# ── Token: redeeming a code ──────────────────────────────────────────


async def _authorized(client, **server_kwargs) -> tuple[str, str, str]:
    """Register, authorize, and return `(client_id, code, verifier)`."""
    record = await _register(client)
    verifier, challenge = _pkce()
    response = await client.get(
        f"/authorize?{_authorize_query(record['client_id'], challenge)}", follow_redirects=False
    )
    return record["client_id"], _code_from(response.headers["location"]), verifier


async def _redeem(client, client_id: str, code: str, verifier: str, **overrides) -> dict:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    form.update(overrides)
    response = await client.post("/token", data=form)
    return json.loads(response.body) | {"_status": response.status_code}


async def test_a_code_is_exchanged_for_a_token():
    async with AsyncTestClient(_app(_server())) as client:
        client_id, code, verifier = await _authorized(client)
        issued = await _redeem(client, client_id, code, verifier)
        assert issued["_status"] == 200
        assert issued["token_type"] == "Bearer"
        assert issued["expires_in"] == 3600
        assert issued["access_token"] and issued["refresh_token"]
        assert issued["scope"] == "mcp:tools"


async def test_a_code_cannot_be_redeemed_twice():
    """The classic replay: the code is taken, not read."""
    async with AsyncTestClient(_app(_server())) as client:
        client_id, code, verifier = await _authorized(client)
        assert (await _redeem(client, client_id, code, verifier))["_status"] == 200
        second = await _redeem(client, client_id, code, verifier)
        assert second["_status"] == 400
        assert second["error"] == "invalid_grant"


async def test_a_wrong_verifier_is_refused():
    """What PKCE is for: the interceptor has the code but not the verifier."""
    async with AsyncTestClient(_app(_server())) as client:
        client_id, code, _verifier = await _authorized(client)
        issued = await _redeem(client, client_id, code, secrets.token_urlsafe(48))
        assert issued["_status"] == 400
        assert issued["error"] == "invalid_grant"


async def test_a_mismatched_redirect_uri_is_refused():
    async with AsyncTestClient(_app(_server())) as client:
        client_id, code, verifier = await _authorized(client)
        issued = await _redeem(
            client, client_id, code, verifier, redirect_uri="https://elsewhere.example/cb"
        )
        assert issued["_status"] == 400


async def test_a_code_issued_to_another_client_is_refused():
    async with AsyncTestClient(_app(_server())) as client:
        _client_id, code, verifier = await _authorized(client)
        other = await _register(client)
        issued = await _redeem(client, other["client_id"], code, verifier)
        assert issued["_status"] == 400
        assert issued["error"] == "invalid_grant"


async def test_an_expired_code_is_refused():
    store = InMemoryAuthorizationStore()
    server = _server(store=store)
    async with AsyncTestClient(_app(server)) as client:
        record = await _register(client)
        verifier, challenge = _pkce()
        await store.save_code(
            _digest("stale-code"),
            AuthorizationCode(
                client_id=record["client_id"],
                redirect_uri=REDIRECT,
                code_challenge=challenge,
                subject="user-42",
                scopes=frozenset({"mcp:tools"}),
                resource=None,
                expires_at=_now() - 1,
            ),
        )
        issued = await _redeem(client, record["client_id"], "stale-code", verifier)
        assert issued["_status"] == 400
        assert issued["error"] == "invalid_grant"


async def test_an_unknown_code_is_refused():
    async with AsyncTestClient(_app(_server())) as client:
        record = await _register(client)
        issued = await _redeem(client, record["client_id"], "never-issued", "whatever")
        assert issued["_status"] == 400


async def test_a_confidential_client_must_present_its_secret():
    async with AsyncTestClient(_app(_server())) as client:
        record = await _register(client, token_endpoint_auth_method="client_secret_post")
        verifier, challenge = _pkce()
        response = await client.get(
            f"/authorize?{_authorize_query(record['client_id'], challenge)}",
            follow_redirects=False,
        )
        code = _code_from(response.headers["location"])

        wrong = await _redeem(client, record["client_id"], code, verifier, client_secret="nope")
        assert wrong["_status"] == 401
        assert wrong["error"] == "invalid_client"


async def test_a_confidential_client_with_its_secret_is_served():
    async with AsyncTestClient(_app(_server())) as client:
        record = await _register(client, token_endpoint_auth_method="client_secret_post")
        verifier, challenge = _pkce()
        response = await client.get(
            f"/authorize?{_authorize_query(record['client_id'], challenge)}",
            follow_redirects=False,
        )
        code = _code_from(response.headers["location"])
        issued = await _redeem(
            client, record["client_id"], code, verifier, client_secret=record["client_secret"]
        )
        assert issued["_status"] == 200


async def test_an_unsupported_grant_type_is_refused():
    async with AsyncTestClient(_app(_server())) as client:
        response = await client.post("/token", data={"grant_type": "password"})
        assert response.status_code == 400
        assert json.loads(response.body)["error"] == "unsupported_grant_type"


# ── Token: refreshing ────────────────────────────────────────────────


async def test_a_refresh_token_yields_a_new_pair():
    async with AsyncTestClient(_app(_server())) as client:
        client_id, code, verifier = await _authorized(client)
        first = await _redeem(client, client_id, code, verifier)
        response = await client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": client_id,
            },
        )
        second = json.loads(response.body)
        assert response.status_code == 200
        assert second["access_token"] != first["access_token"]
        assert second["refresh_token"] != first["refresh_token"]


async def test_a_used_refresh_token_stops_working():
    """Rotation: a stolen refresh token is useful only until the real client refreshes."""
    async with AsyncTestClient(_app(_server())) as client:
        client_id, code, verifier = await _authorized(client)
        first = await _redeem(client, client_id, code, verifier)
        form = {
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_id,
        }
        assert (await client.post("/token", data=form)).status_code == 200
        replayed = await client.post("/token", data=form)
        assert replayed.status_code == 400


async def test_a_refresh_token_belonging_to_another_client_is_refused():
    async with AsyncTestClient(_app(_server())) as client:
        client_id, code, verifier = await _authorized(client)
        first = await _redeem(client, client_id, code, verifier)
        other = await _register(client)
        response = await client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": other["client_id"],
            },
        )
        assert response.status_code == 400


# ── Validating what was issued ───────────────────────────────────────


async def test_the_verifier_resolves_a_token_to_its_principal():
    server = _server()
    async with AsyncTestClient(_app(server)) as client:
        client_id, code, verifier = await _authorized(client)
        issued = await _redeem(client, client_id, code, verifier)
        principal = await server.verifier()(issued["access_token"])
        assert principal is not None
        assert principal.subject == "user-42"
        assert principal.scopes == frozenset({"mcp:tools"})
        assert principal.claims["client_id"] == client_id


def _advance(monkeypatch, seconds: float) -> None:
    """Move the authorization module's clock forward.

    `_now()` is the one clock the module reads, so shifting it is exact - and a
    real `asyncio.sleep` proving a 1ms TTL costs wall time and depends on the
    sleep being long enough on a loaded machine.
    """
    # The module object, because the helper patches an attribute on it -
    # importing `_now` by name would give a value, not the binding to swap.
    from veloce.contrib.mcp import authorization

    base = authorization._now()
    monkeypatch.setattr(authorization, "_now", lambda: base + seconds)


async def test_the_verifier_refuses_a_token_it_never_issued():
    server = _server()
    assert await server.verifier()(secrets.token_urlsafe(32)) is None


async def test_the_verifier_refuses_an_expired_token(monkeypatch):
    """The clock moves, not the test: a real sleep is slow and inexact."""
    server = _server(access_token_ttl=60)
    async with AsyncTestClient(_app(server)) as client:
        client_id, code, verifier = await _authorized(client)
        issued = await _redeem(client, client_id, code, verifier)
        assert await server.verifier()(issued["access_token"]) is not None

        _advance(monkeypatch, 61)
        assert await server.verifier()(issued["access_token"]) is None


async def test_an_expired_token_is_dropped_rather_than_kept(monkeypatch):
    store = InMemoryAuthorizationStore()
    server = _server(access_token_ttl=60, store=store)
    async with AsyncTestClient(_app(server)) as client:
        client_id, code, verifier = await _authorized(client)
        issued = await _redeem(client, client_id, code, verifier)
        assert await store.get_token(_digest(issued["access_token"])) is not None

        _advance(monkeypatch, 61)
        await server.verifier()(issued["access_token"])
        assert await store.get_token(_digest(issued["access_token"])) is None


async def test_only_the_token_digest_is_stored():
    """A leaked store yields digests, not credentials."""
    store = InMemoryAuthorizationStore()
    server = _server(store=store)
    async with AsyncTestClient(_app(server)) as client:
        client_id, code, verifier = await _authorized(client)
        issued = await _redeem(client, client_id, code, verifier)
        assert await store.get_token(_digest(issued["access_token"])) is not None
        assert await store.get_token(issued["access_token"]) is None


async def test_the_requested_resource_is_recorded_on_the_token():
    """RFC 8707: a token minted for one server should not be replayed at another."""
    store = InMemoryAuthorizationStore()
    server = _server(store=store)
    async with AsyncTestClient(_app(server)) as client:
        record = await _register(client)
        verifier, challenge = _pkce()
        query = _authorize_query(record["client_id"], challenge, resource=f"{ISSUER}/mcp")
        response = await client.get(f"/authorize?{query}", follow_redirects=False)
        code = _code_from(response.headers["location"])
        issued = await _redeem(client, record["client_id"], code, verifier)
        principal = await server.verifier()(issued["access_token"])
        assert principal is not None
        assert principal.claims["aud"] == f"{ISSUER}/mcp"


# ── The store contract ───────────────────────────────────────────────


def test_the_bundled_store_satisfies_the_protocol():

    assert isinstance(InMemoryAuthorizationStore(), AuthorizationStore)


async def test_a_code_is_removed_by_taking_it():
    store = InMemoryAuthorizationStore()
    code = AuthorizationCode(
        client_id="c",
        redirect_uri=REDIRECT,
        code_challenge="x",
        subject="s",
        scopes=frozenset(),
        resource=None,
        expires_at=_now() + 60,
    )
    await store.save_code("digest", code)
    assert await store.take_code("digest") is code
    assert await store.take_code("digest") is None


def test_a_client_without_a_secret_reports_itself_public():
    assert OAuthClient(client_id="c", redirect_uris=(REDIRECT,)).is_public is True
    assert (
        OAuthClient(client_id="c", redirect_uris=(REDIRECT,), client_secret_digest="d").is_public
        is False
    )


# ── PKCE S256 verification (RFC 7636 Sec. 4.2) ───────────────────────
#
# The challenge is BASE64URL-ENCODE(SHA256(verifier)): the RFC 4648 Sec. 5
# alphabet with padding stripped. These pin the encoding itself, so the
# comparison cannot start accepting a differently-spelled challenge.


def test_the_rfc_7636_appendix_b_vector_verifies():
    """The worked example from the specification, end to end."""
    assert _verify_pkce(
        "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    )


def test_a_challenge_carrying_base64_padding_is_refused():
    """RFC 7636 Sec. 4.2 strips `=`; a padded spelling is a different string
    and `hmac.compare_digest` must not treat it as equal."""
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert not _verify_pkce(verifier, challenge + "=")
    assert not _verify_pkce(verifier, challenge + "==")


def test_a_standard_base64_challenge_is_refused():
    """The `+` / `/` alphabet is not the URL-safe one RFC 4648 Sec. 5 names."""
    verifier = "v" * 43
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    standard = base64.b64encode(digest).rstrip(b"=").decode("ascii")
    urlsafe = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert standard != urlsafe  # the vector actually exercises the difference
    assert _verify_pkce(verifier, urlsafe)
    assert not _verify_pkce(verifier, standard)


def test_the_challenge_is_43_unpadded_characters_for_every_verifier():
    """SHA-256 is 32 bytes, which is 42.67 base64 characters - always one
    padding character stripped, so the challenge is always 43 long."""
    for length in (43, 64, 96, 128):
        verifier = secrets.token_urlsafe(length)[:length]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert len(challenge) == 43
        assert "=" not in challenge
        assert _verify_pkce(verifier, challenge)


def test_a_verifier_one_character_off_is_refused():
    verifier, challenge = _pkce()
    assert _verify_pkce(verifier, challenge)
    assert not _verify_pkce(verifier[:-1] + ("A" if verifier[-1] != "A" else "B"), challenge)


def test_an_empty_challenge_is_refused():
    verifier, _challenge = _pkce()
    assert not _verify_pkce(verifier, "")
