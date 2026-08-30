"""A registered client gets the grant types it asked for, and only those.

`OAuthClient.grant_types` existed, was never read from the registration request,
and was never checked at the token endpoint. Three things followed:

    POST /register {"grant_types": ["authorization_code"]}
      201 says      grant_types: ["authorization_code","refresh_token"]   <- a lie
      store holds   ("authorization_code","refresh_token")                <- request dropped
      POST /token grant_type=refresh_token  ->  400 invalid_grant         <- reached the
                                                                             token lookup

The 201 echoed a hardcoded pair regardless of what was asked, so a client that
registered for the code grant alone was told it also held refresh. And because no
grant function consulted `client.grant_types`, it did. A deployment with a durable
`AuthorizationStore` could persist `grant_types=("authorization_code",)` by hand
and refresh still worked for that client.

After the fix the registration is the contract: what is asked for is stored, what
is stored is echoed, and a grant the client does not hold is refused with
`unauthorized_client` (RFC 6749 Sec. 5.2) before any token is looked up.

**One deliberate deviation from RFC 7591 Sec. 2.** The spec says an omitted
`grant_types` defaults to `["authorization_code"]` alone. Veloce keeps the
historical default of both, because narrowing it silently would stop refresh
working for every client already registered without the key. A client that wants
the narrow set asks for it, and now gets it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from tests._mcp_source import compared_constants, tree
from veloce import Principal, Veloce
from veloce.contrib.mcp.authorization import (
    SUPPORTED_GRANT_TYPES,
    InMemoryAuthorizationStore,
    MCPAuthorizationServer,
    register_authorization_server,
)
from veloce.testclient import TestClient

REDIRECT = "https://client.example/cb"
#: A fixed S256 pair, so the flow does not depend on a generator.
VERIFIER = "a" * 64


def _build() -> tuple[InMemoryAuthorizationStore, TestClient]:
    store = InMemoryAuthorizationStore()
    server = MCPAuthorizationServer(
        issuer="https://auth.example.com",
        store=store,
        authenticate=lambda request: Principal(subject="user-1", scopes={"mcp:tools"}),
        allow_dynamic_registration=True,
        scopes_supported=("mcp:tools",),
    )
    app = Veloce(openapi_url=None)
    register_authorization_server(app, server)
    return store, TestClient(app)


def _stored(store: InMemoryAuthorizationStore, client_id: str):
    """Read a client out of the store from a sync test.

    The store holds plain objects with no loop affinity, and `TestClient` owns
    the loop an `async def` test would already be running on.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(store.get_client(client_id))
    finally:
        loop.close()


def _register(client: TestClient, **extra) -> dict:
    body = {"redirect_uris": [REDIRECT], **extra}
    return client.post("/register", json=body).json()


def _challenge() -> str:
    digest = hashlib.sha256(VERIFIER.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


# ── what is asked for is what is stored ──────────────────────────────


def test_a_narrow_registration_is_stored_narrow():
    """The defect: the request was dropped and both grants were stored."""
    store, client = _build()
    registered = _register(client, grant_types=["authorization_code"])
    held = _stored(store, registered["client_id"])
    assert held is not None
    assert held.grant_types == ("authorization_code",)


def test_the_response_echoes_what_was_stored():
    """The defect: a hardcoded pair, so the client was told it held refresh."""
    _store, client = _build()
    assert _register(client, grant_types=["authorization_code"])["grant_types"] == [
        "authorization_code"
    ]


def test_a_full_registration_is_stored_in_full():
    _store, client = _build()
    registered = _register(client, grant_types=["authorization_code", "refresh_token"])
    assert registered["grant_types"] == ["authorization_code", "refresh_token"]


def test_an_omitted_grant_types_keeps_both():
    """The documented deviation from RFC 7591, pinned so it stays deliberate."""
    store, client = _build()
    registered = _register(client)
    assert registered["grant_types"] == list(SUPPORTED_GRANT_TYPES)
    held = _stored(store, registered["client_id"])
    assert held is not None
    assert held.grant_types == SUPPORTED_GRANT_TYPES


# ── a grant the client does not hold is refused ──────────────────────


def test_refresh_is_refused_for_a_code_only_client():
    """The defect: this reached the token lookup and answered `invalid_grant`."""
    _store, client = _build()
    registered = _register(client, grant_types=["authorization_code"])
    response = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "anything",
            "client_id": registered["client_id"],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "unauthorized_client"


def test_the_refusal_names_the_grant():
    _store, client = _build()
    registered = _register(client, grant_types=["authorization_code"])
    response = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "anything",
            "client_id": registered["client_id"],
        },
    )
    assert "refresh_token" in response.json()["error_description"]


def test_refresh_is_allowed_for_a_client_that_registered_for_it():
    """The negative: refusing everything would pass the test above vacuously."""
    _store, client = _build()
    registered = _register(client, grant_types=["authorization_code", "refresh_token"])
    response = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "not-a-real-token",
            "client_id": registered["client_id"],
        },
    )
    # Past the grant gate, refused on the token itself.
    assert response.json()["error"] == "invalid_grant"


def test_the_gate_runs_before_the_token_is_looked_up():
    """An unregistered grant must not leak whether a token exists."""
    _store, client = _build()
    narrow = _register(client, grant_types=["authorization_code"])
    for refresh in ("definitely-not-real", ""):
        response = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": narrow["client_id"],
            },
        )
        assert response.json()["error"] == "unauthorized_client"


# ── the registration itself is validated ─────────────────────────────


@pytest.mark.parametrize(
    "grants",
    [["password"], ["client_credentials"], ["authorization_code", "implicit"], ["urn:made:up"]],
)
def test_an_unsupported_grant_type_is_refused(grants):
    """Silently narrowing to what is implemented is the drop this replaces."""
    _store, client = _build()
    response = client.post("/register", json={"redirect_uris": [REDIRECT], "grant_types": grants})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"


def test_the_refusal_names_the_unsupported_grant():
    _store, client = _build()
    response = client.post(
        "/register", json={"redirect_uris": [REDIRECT], "grant_types": ["password"]}
    )
    assert "password" in response.json()["error_description"]


@pytest.mark.parametrize("grants", ["authorization_code", 5, {"a": 1}, [1, 2], [None]])
def test_a_malformed_grant_types_is_refused(grants):
    _store, client = _build()
    response = client.post("/register", json={"redirect_uris": [REDIRECT], "grant_types": grants})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"


def test_a_registration_without_the_code_grant_is_refused():
    """`refresh_token` alone can never obtain a first token."""
    _store, client = _build()
    response = client.post(
        "/register", json={"redirect_uris": [REDIRECT], "grant_types": ["refresh_token"]}
    )
    assert response.status_code == 400
    assert "authorization_code" in response.json()["error_description"]


def test_an_empty_grant_types_is_refused():
    _store, client = _build()
    response = client.post("/register", json={"redirect_uris": [REDIRECT], "grant_types": []})
    assert response.status_code == 400


# ── the code grant still works end to end ────────────────────────────


def _authorize(client: TestClient, client_id: str) -> str:
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": _challenge(),
            "code_challenge_method": "S256",
        },
    )
    location = response.headers["Location"]
    return parse_qs(urlparse(location).query)["code"][0]


@pytest.mark.parametrize(
    "grants", [["authorization_code"], ["authorization_code", "refresh_token"], None]
)
def test_the_code_grant_still_issues_a_token(grants):
    """The gate must not break the grant every client holds."""
    _store, client = _build()
    extra = {} if grants is None else {"grant_types": grants}
    registered = _register(client, **extra)
    code = _authorize(client, registered["client_id"])
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "client_id": registered["client_id"],
        },
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


def test_a_code_only_clients_refresh_token_is_unusable():
    """Named for what it checks.

    It used to be `test_a_code_only_client_gets_no_refresh_token`, which is a
    condition that never holds: `_issue_tokens` puts a `refresh_token` in every
    response unconditionally. The test guarded its real assertion behind
    `if "refresh_token" in body:`, so the branch it existed for ran while the
    name said the opposite - and had the server ever stopped issuing one, the
    test would have passed by taking neither path.

    What matters is not whether the token exists but that this client cannot
    redeem it, which is now asserted directly.
    """
    _store, client = _build()
    registered = _register(client, grant_types=["authorization_code"])
    code = _authorize(client, registered["client_id"])
    body = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "client_id": registered["client_id"],
        },
    ).json()
    assert body["access_token"]
    # Asserted, not branched on: the token is always issued, so a conditional
    # here only hides a regression that stopped issuing it.
    assert "refresh_token" in body
    refreshed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
            "client_id": registered["client_id"],
        },
    )
    assert refreshed.json()["error"] == "unauthorized_client"


def test_a_full_client_can_refresh_end_to_end():
    """The whole point of holding the grant."""
    _store, client = _build()
    registered = _register(client, grant_types=["authorization_code", "refresh_token"])
    code = _authorize(client, registered["client_id"])
    first = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "client_id": registered["client_id"],
        },
    ).json()
    # Asserted rather than skipped. A `pytest.skip` here turns the regression
    # this test exists to catch - the server no longer issuing a refresh token -
    # into a silent pass, which is the one outcome that must not be quiet.
    assert "refresh_token" in first, "the token endpoint stopped issuing a refresh token"
    second = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": registered["client_id"],
        },
    )
    assert second.status_code == 200
    assert second.json()["access_token"] != first["access_token"]


# ── an unknown grant type is still an unsupported one ────────────────


def test_an_unknown_grant_type_at_the_token_endpoint_is_unchanged():
    _store, client = _build()
    registered = _register(client)
    response = client.post(
        "/token", data={"grant_type": "password", "client_id": registered["client_id"]}
    )
    assert response.json()["error"] == "unsupported_grant_type"


def test_the_supported_set_matches_what_the_endpoint_implements():
    """A grant accepted at registration that `/token` cannot serve is a dead end."""
    dispatched = compared_constants(tree("authorization.py"), "grant_type")
    assert dispatched == set(SUPPORTED_GRANT_TYPES)
