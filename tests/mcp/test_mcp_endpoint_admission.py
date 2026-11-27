"""Admission control on the MCP endpoint applies to every verb it serves.

The Streamable HTTP transport is one route answering POST, GET and DELETE. Three
rules gate it: the `Origin` allowlist (DNS-rebinding defense), the
`MCP-Protocol-Version` header, and bearer authentication. Each rule had a single
implementation, and each was invoked from the POST handler only - so a `GET`
replayed another principal's tool output with no credential, and a `DELETE`
terminated a live session with neither a credential nor an `Origin` check.

The checks now run once above the verb switch, so a verb added later inherits
them rather than having to remember three calls.
"""

from __future__ import annotations

import orjson
import pytest

from tests._mcp import auth
from veloce import MCPContext, Principal, Veloce
from veloce.contrib.mcp import MCPAuth

_SECRET = {"ssn": "078-05-1120"}


def _verify(token: str):
    if token == "good":
        return Principal(subject="agent-1", scopes=frozenset({"mcp:tools"}))
    return None


def _auth() -> MCPAuth:
    return auth(_verify)


_TOKEN = {"authorization": "Bearer good"}


def _app(*, auth=True, **mount) -> Veloce:
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Return a secret")
    async def secret() -> dict:
        return dict(_SECRET)

    @app.mcp_tool(description="Work with progress")
    async def work(ctx: MCPContext) -> str:
        await ctx.report_progress(1, 2)
        return "done"

    app.mount_mcp(transport="http", auth=_auth() if auth else None, **mount)
    return app


def _call(name: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": {}},
    }


def _init() -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


def _sse_ids(body: bytes) -> list[str]:
    return [
        line.split(b":", 1)[1].strip().decode()
        for line in body.splitlines()
        if line.startswith(b"id:")
    ]


# ── Authentication reaches every verb ────────────────────────────────


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("post", {"json": _call("secret")}),
        ("get", {"headers": {"last-event-id": "anything.0"}}),
        ("delete", {"headers": {"mcp-session-id": "whatever"}}),
    ],
)
def test_every_verb_refuses_a_missing_token(method, kwargs):
    client = _app(sessions=True, resumable=True).test_client()
    resp = getattr(client, method)("/mcp", **kwargs)
    assert resp.status_code == 401, method
    assert "Bearer" in resp.headers.get("www-authenticate", "")


@pytest.mark.parametrize("method", ["post", "get", "delete"])
def test_every_verb_refuses_an_invalid_token(method):
    client = _app(sessions=True, resumable=True).test_client()
    resp = getattr(client, method)(
        "/mcp",
        headers={"authorization": "Bearer nope", "last-event-id": "x.0"},
        **({"json": _call("secret")} if method == "post" else {}),
    )
    assert resp.status_code == 401, method


# ── The two defects, end to end ──────────────────────────────────────


def test_an_unauthenticated_replay_cannot_read_another_principals_output():
    """The reported defect: a GET replayed a tool result with no credential."""
    app = _app(resumable=True)
    client = app.test_client()

    streamed = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "work", "arguments": {}, "_meta": {"progressToken": "p"}},
        },
        headers={**_TOKEN, "accept": "text/event-stream"},
    )
    assert streamed.status_code == 200
    event_id = _sse_ids(streamed.body)[0]

    # A second client holding only the event id gets nothing.
    replay = client.get("/mcp", headers={"last-event-id": event_id})
    assert replay.status_code == 401
    assert b"done" not in replay.body

    # The same replay with a credential still works, so the fix did not simply
    # disable resumption.
    allowed = client.get("/mcp", headers={**_TOKEN, "last-event-id": event_id})
    assert allowed.status_code == 200
    assert b"done" in allowed.body


def test_an_unauthenticated_delete_cannot_terminate_a_live_session():
    """The reported defect: a DELETE terminated a session with no credential."""
    app = _app(sessions=True)
    client = app.test_client()
    session_id = client.post("/mcp", json=_init(), headers=_TOKEN).headers["mcp-session-id"]

    refused = client.delete("/mcp", headers={"mcp-session-id": session_id})
    assert refused.status_code == 401

    # The session survived: it still answers.
    still_live = client.post(
        "/mcp", json=_call("secret"), headers={**_TOKEN, "mcp-session-id": session_id}
    )
    assert still_live.status_code == 200

    # With a credential the same DELETE terminates it, and the id stops working.
    assert (
        client.delete("/mcp", headers={**_TOKEN, "mcp-session-id": session_id}).status_code == 204
    )
    gone = client.post(
        "/mcp", json=_call("secret"), headers={**_TOKEN, "mcp-session-id": session_id}
    )
    assert gone.status_code == 404


# ── The Origin allowlist reaches every verb ──────────────────────────


@pytest.mark.parametrize("method", ["post", "get", "delete"])
def test_every_verb_refuses_a_disallowed_origin(method):
    client = _app(
        auth=False, sessions=True, resumable=True, allowed_origins=["https://good.example"]
    ).test_client()
    resp = getattr(client, method)(
        "/mcp",
        headers={"origin": "https://evil.example", "last-event-id": "x.0"},
        **({"json": _call("secret")} if method == "post" else {}),
    )
    assert resp.status_code == 403, method
    assert orjson.loads(resp.body)["error"]["message"] == "origin not allowed"


@pytest.mark.parametrize("method", ["post", "delete"])
def test_an_allowed_origin_still_passes(method):
    """The gate rejects an origin, it does not reject the verb."""
    client = _app(auth=False, sessions=True, allowed_origins=["https://good.example"]).test_client()
    resp = getattr(client, method)(
        "/mcp",
        headers={"origin": "https://good.example", "mcp-session-id": "unknown"},
        **({"json": _call("secret")} if method == "post" else {}),
    )
    assert resp.status_code != 403, method


# ── The protocol-version header reaches every verb ───────────────────


@pytest.mark.parametrize("method", ["post", "get", "delete"])
def test_every_verb_refuses_an_unsupported_protocol_version(method):
    client = _app(auth=False, sessions=True, resumable=True).test_client()
    resp = getattr(client, method)(
        "/mcp",
        headers={"mcp-protocol-version": "1999-01-01", "last-event-id": "x.0"},
        **({"json": _call("secret")} if method == "post" else {}),
    )
    assert resp.status_code == 400, method
    assert "unsupported" in orjson.loads(resp.body)["error"]["message"]


# ── Ordering, and what must NOT be gated ─────────────────────────────


def test_the_header_checks_precede_token_verification():
    """A cheap unconditional check should not cost a token round trip."""
    client = _app(allowed_origins=["https://good.example"]).test_client()
    resp = client.post("/mcp", json=_call("secret"), headers={"origin": "https://evil.example"})
    # No credential was sent, yet the origin decides: 403, not 401.
    assert resp.status_code == 403


def test_discovery_metadata_stays_reachable_without_a_credential():
    """A client cannot present a token before learning where to get one."""
    client = _app().test_client()
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    assert "authorization_servers" in orjson.loads(resp.body)


def test_an_endpoint_with_no_auth_configured_still_serves_every_verb():
    """Hoisting the checks must not require credentials no one configured."""
    client = _app(auth=False, sessions=True, resumable=True).test_client()
    session_id = client.post("/mcp", json=_init()).headers["mcp-session-id"]
    assert (
        client.post(
            "/mcp", json=_call("secret"), headers={"mcp-session-id": session_id}
        ).status_code
        == 200
    )
    # Unsupported / not-found rather than refused: neither verb demands a credential.
    assert client.get("/mcp").status_code == 405
    assert client.delete("/mcp", headers={"mcp-session-id": "nope"}).status_code == 404
    assert client.delete("/mcp", headers={"mcp-session-id": session_id}).status_code == 204


def test_an_authenticated_call_still_reaches_the_tool():
    client = _app().test_client()
    resp = client.post("/mcp", json=_call("secret"), headers=_TOKEN)
    assert resp.status_code == 200
    payload = orjson.loads(resp.body)["result"]["content"][0]["text"]
    assert orjson.loads(payload) == _SECRET
