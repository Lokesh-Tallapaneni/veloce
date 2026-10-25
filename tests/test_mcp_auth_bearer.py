"""Validating a static bearer token without being an OAuth resource server.

`MCPAuth` required both `resource_server_url` and `authorization_servers`, which
is correct for an OAuth resource server: the RFC 9728 metadata document it
serves has to name the resource a client audience-binds its token to and the
server the token comes from, or a compliant client cannot act on it.

A local daemon holding one pre-shared token has neither, so it could not use
`MCPAuth` at all — the one thing it wanted, "check this token", was reachable
only by inventing two URLs that describe nothing. `MCPAuth.bearer` is that mode:
tokens are still verified and refusals still carry a challenge, but no metadata
document is served and none is required of the configuration.

The OAuth mode is unchanged, which these tests also pin: the requirement that
produced the defect is the right requirement for the deployment it was written
for.
"""

from __future__ import annotations

import orjson
import pytest

from tests._mcp import AUTHORIZATION_SERVER_URL, RESOURCE_SERVER_URL, auth
from veloce import Veloce
from veloce.contrib.mcp import MCPAuth
from veloce.contrib.mcp.auth import PROTECTED_RESOURCE_METADATA_PATH
from veloce.principal import Principal, current_principal

SCOPES = ["mcp:tools"]


def _verify(token: str) -> Principal | None:
    """Accept the one pre-shared token, carrying the scopes it was issued with."""
    if token == "shared-secret":
        return Principal(subject="daemon", scopes=frozenset(SCOPES))
    if token == "unscoped":
        return Principal(subject="daemon")
    return None


def _app(**mount: object) -> Veloce:
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Name the caller")
    async def whoami() -> str:
        principal = current_principal()
        return principal.subject if principal else "anon"

    app.mount_mcp(transport="http", **mount)
    return app


def _bearer_app() -> Veloce:
    return _app(auth=MCPAuth.bearer(verify=_verify, required_scopes=SCOPES))


def _call(app: Veloce, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return app.test_client().post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}},
        },
        headers=headers,
    )


# ── the mode constructs and admits ───────────────────────────────────


def test_bearer_constructs_without_any_oauth_fields():
    """The defect: this raised `ValueError` and there was no other door."""
    config = MCPAuth.bearer(verify=_verify)
    assert config.bearer_only is True
    assert config.resource_server_url is None
    assert config.authorization_servers == ()


def test_bearer_normalizes_its_required_scopes():
    assert MCPAuth.bearer(verify=_verify, required_scopes=SCOPES).required_scopes == frozenset(
        SCOPES
    )


def test_a_valid_token_is_accepted():
    response = _call(_bearer_app(), "shared-secret")
    assert response.status_code == 200


def test_the_principal_is_published_to_the_tool():
    body = orjson.loads(_call(_bearer_app(), "shared-secret").body)
    assert body["result"]["content"][0]["text"] == "daemon"


# ── and still refuses ────────────────────────────────────────────────


def test_a_missing_token_is_refused():
    assert _call(_bearer_app()).status_code == 401


def test_a_missing_token_still_gets_a_challenge():
    """Dropping the metadata pointer must not drop the challenge itself."""
    challenge = _call(_bearer_app()).headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'error="invalid_token"' in challenge


def test_an_invalid_token_is_refused():
    assert _call(_bearer_app(), "wrong").status_code == 401


def test_a_token_short_of_the_required_scopes_is_forbidden():
    response = _call(_bearer_app(), "unscoped")
    assert response.status_code == 403
    assert 'error="insufficient_scope"' in response.headers["WWW-Authenticate"]


def test_the_forbidden_challenge_names_the_scopes_it_wanted():
    challenge = _call(_bearer_app(), "unscoped").headers["WWW-Authenticate"]
    assert 'scope="mcp:tools"' in challenge


# ── no metadata document is served, or promised ──────────────────────


def test_no_metadata_route_is_registered():
    """There is no resource URI and no authorization server to describe."""
    client = _bearer_app().test_client()
    assert client.get(PROTECTED_RESOURCE_METADATA_PATH).status_code == 404


def test_the_challenge_does_not_point_at_the_absent_document():
    """A pointer a client cannot follow is worse than no pointer at all."""
    challenge = _call(_bearer_app()).headers["WWW-Authenticate"]
    assert "resource_metadata" not in challenge


def test_building_the_metadata_document_is_refused():
    with pytest.raises(RuntimeError, match="no protected-resource metadata"):
        MCPAuth.bearer(verify=_verify).metadata()


def test_bearer_mode_refuses_oauth_fields_it_would_silently_drop():
    with pytest.raises(ValueError, match="serves no protected-resource metadata"):
        MCPAuth(verify=_verify, bearer_only=True, resource_server_url=RESOURCE_SERVER_URL)


# ── the OAuth mode is unchanged ──────────────────────────────────────


def test_the_oauth_mode_still_serves_its_metadata_document():
    client = _app(auth=auth()).test_client()
    document = orjson.loads(client.get(PROTECTED_RESOURCE_METADATA_PATH).body)
    assert document["resource"] == RESOURCE_SERVER_URL
    assert document["authorization_servers"] == [AUTHORIZATION_SERVER_URL]


def test_the_oauth_challenge_still_points_at_that_document():
    challenge = _call(_app(auth=auth())).headers["WWW-Authenticate"]
    assert f'resource_metadata="{PROTECTED_RESOURCE_METADATA_PATH}"' in challenge


# ── the two modes coexist without leaking ────────────────────────────


def test_two_apps_in_one_process_keep_their_own_routing_tables():
    """The metadata path is fixed by the RFC, so only per-app routing separates them."""
    bearer, oauth = _bearer_app(), _app(auth=auth())
    assert bearer.test_client().get(PROTECTED_RESOURCE_METADATA_PATH).status_code == 404
    assert oauth.test_client().get(PROTECTED_RESOURCE_METADATA_PATH).status_code == 200


def test_building_the_oauth_app_first_does_not_give_the_bearer_app_a_route():
    """Registration order must not decide it; nothing is shared but the path."""
    oauth = _app(auth=auth())
    assert oauth.test_client().get(PROTECTED_RESOURCE_METADATA_PATH).status_code == 200
    assert _bearer_app().test_client().get(PROTECTED_RESOURCE_METADATA_PATH).status_code == 404


def test_one_app_mounting_both_modes_serves_the_document_for_the_oauth_mount():
    """The fixed path is the OAuth mount's; the bearer mount adds and claims nothing."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Name the caller")
    async def whoami() -> str:
        principal = current_principal()
        return principal.subject if principal else "anon"

    app.mount_mcp(transport="http", path="/local", auth=MCPAuth.bearer(verify=_verify))
    app.mount_mcp(transport="http", path="/mcp", auth=auth())
    client = app.test_client()
    assert client.get(PROTECTED_RESOURCE_METADATA_PATH).status_code == 200
    local = client.post("/local", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert local.status_code == 401
    assert "resource_metadata" not in local.headers["WWW-Authenticate"]
    remote = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert (
        f'resource_metadata="{PROTECTED_RESOURCE_METADATA_PATH}"'
        in remote.headers["WWW-Authenticate"]
    )


def test_a_missing_resource_server_url_is_still_a_configuration_error():
    with pytest.raises(ValueError, match="resource_server_url"):
        MCPAuth(verify=_verify)


def test_a_missing_authorization_server_is_still_a_configuration_error():
    with pytest.raises(ValueError, match="authorization_servers"):
        MCPAuth(verify=_verify, resource_server_url=RESOURCE_SERVER_URL)
