"""Three guarantees the prose made that the code did not keep.

**1. RFC 8707 audience binding.** `authorization.py` stated that the `resource`
parameter "is recorded on the token and checked on validation, so a token minted
for one MCP server cannot be replayed against another". The verifier accepted a
`resource` argument and never read it, and never compared `record.resource`
against anything:

    token bound to https://a.example/mcp
      verify(tok)                          -> u1
      verify(tok, "https://b.example/mcp") -> u1     <- replay accepted

Audience binding is the control that stops a token obtained for one MCP server
being replayed against another that shares an authorization server. It now needs
this server's own URI to compare against, and says so loudly when it is not given
rather than claiming a check it cannot perform.

**2. `tool_filter` is not an authorization boundary.** `_candidate_tools` said
"an unlisted tool still raises `AuthorizationError`". It does not - and
`MCPContext.hide`, in the same codebase, documents the true rule ("Hiding is not
enforcement"). The code matches `hide`; the other docstring was the false one, and
it was false in the dangerous direction: an operator reading it would use
`tool_filter` as a permission boundary.

    tool_filter hiding everything
      tools/list -> {"tools": []}
      tools/call -> "RAN THE HIDDEN TOOL"

The behaviour is deliberate and unchanged - scopes are the boundary - so this
pins the behaviour *and* that the two docstrings now agree.

**3. `@app.middleware("http", **kwargs)` dropped every option.** The decorator
branch never touched `kwargs`. `priority=` and `name=` are real `add_middleware`
options, so the near miss gave an author unordered or unnamed middleware with
nothing said anywhere.
"""

from __future__ import annotations

import warnings

import pytest

from veloce import Middleware, Veloce
from veloce.contrib.mcp.authorization import (
    AccessToken,
    InMemoryAuthorizationStore,
    MCPAuthorizationServer,
    _digest,
    _now,
)
from veloce.testclient import TestClient

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}

A_RESOURCE = "https://a.example/mcp"
B_RESOURCE = "https://b.example/mcp"


# ── 1. audience binding ──────────────────────────────────────────────


def _server() -> tuple[MCPAuthorizationServer, InMemoryAuthorizationStore]:
    store = InMemoryAuthorizationStore()
    return (
        MCPAuthorizationServer(
            issuer="https://auth.example.com", store=store, authenticate=lambda request: None
        ),
        store,
    )


async def _mint(store: InMemoryAuthorizationStore, token: str, resource: str | None) -> None:
    await store.save_token(
        _digest(token),
        AccessToken(
            client_id="c",
            subject="u1",
            scopes=frozenset(),
            resource=resource,
            expires_at=_now() + 3600,
        ),
    )


async def test_a_token_for_another_server_is_refused():
    """The defect: this returned the principal."""
    server, store = _server()
    await _mint(store, "tok", A_RESOURCE)
    assert await server.verifier(resource=B_RESOURCE)("tok") is None


async def test_a_token_for_this_server_is_accepted():
    """The negative: refusing everything would pass the test above vacuously."""
    server, store = _server()
    await _mint(store, "tok", A_RESOURCE)
    principal = await server.verifier(resource=A_RESOURCE)("tok")
    assert principal is not None
    assert principal.subject == "u1"


async def test_an_unbound_token_is_still_accepted():
    """A token that named no resource was never audience-bound."""
    server, store = _server()
    await _mint(store, "tok", None)
    principal = await server.verifier(resource=A_RESOURCE)("tok")
    assert principal is not None


async def test_the_audience_reaches_the_principal_claims():
    server, store = _server()
    await _mint(store, "tok", A_RESOURCE)
    principal = await server.verifier(resource=A_RESOURCE)("tok")
    assert principal is not None
    assert principal.claims["aud"] == A_RESOURCE


async def test_an_expired_token_is_still_refused():
    """The check that already worked must keep working."""
    server, store = _server()
    await store.save_token(
        _digest("old"),
        AccessToken(
            client_id="c",
            subject="u1",
            scopes=frozenset(),
            resource=A_RESOURCE,
            expires_at=_now() - 1,
        ),
    )
    assert await server.verifier(resource=A_RESOURCE)("old") is None


async def test_an_unknown_token_is_still_refused():
    server, _store = _server()
    assert await server.verifier(resource=A_RESOURCE)("never-minted") is None


def test_building_a_verifier_without_a_resource_warns():
    """Silence would be the bug: the check cannot run and the docs promised it."""
    server, _store = _server()
    with pytest.warns(UserWarning, match="audience binding"):
        server.verifier()


def test_building_a_verifier_with_a_resource_does_not_warn():
    server, _store = _server()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        server.verifier(resource=A_RESOURCE)


async def test_an_unbound_verifier_still_resolves_a_token():
    """Warning, not breaking: an existing caller keeps working."""
    server, store = _server()
    await _mint(store, "tok", A_RESOURCE)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        verify = server.verifier()
    principal = await verify("tok")
    assert principal is not None


# ── 2. a filtered tool is hidden, not forbidden ──────────────────────


def _filtered_app(tool_filter):
    app = Veloce(title="F", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="A tool")
    async def op() -> str:
        return "ran"

    app.mount_mcp(transport="http", path="/mcp", tool_filter=tool_filter)
    client = TestClient(app)
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    return client


def test_a_filtered_tool_is_absent_from_the_listing():
    client = _filtered_app(lambda tool, principal: False)
    listed = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json"},
    ).json()
    assert listed["result"]["tools"] == []


def test_a_filtered_tool_is_still_callable():
    """Deliberate: scopes are the boundary. Pinned so the docs cannot drift back."""
    client = _filtered_app(lambda tool, principal: False)
    called = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "op"}},
        headers={"Accept": "application/json"},
    ).json()
    assert called["result"]["content"][0]["text"] == "ran"


def test_an_unfiltered_tool_is_listed():
    client = _filtered_app(lambda tool, principal: True)
    listed = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json"},
    ).json()
    assert [t["name"] for t in listed["result"]["tools"]] == ["op"]


def test_the_two_docstrings_agree_that_hiding_is_not_enforcement():
    """The defect was a contradiction; this fails if either side drifts back."""
    from veloce.contrib.mcp.context import MCPContext
    from veloce.contrib.mcp.server import MCPServer

    hide_doc = MCPContext.hide.__doc__ or ""
    candidates_doc = MCPServer._candidate_tools.__doc__ or ""
    assert "not enforcement" in hide_doc
    assert "not enforcement" in candidates_doc
    assert "still raises" not in candidates_doc


# ── 3. the decorator form refuses options it cannot honour ───────────


@pytest.mark.parametrize("kwargs", [{"priority": 99}, {"name": "zzz"}, {"bogus_option": 123}])
def test_the_http_decorator_form_refuses_options(kwargs):
    """The defect: every one of these was accepted and dropped."""
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="takes no options"):
        app.middleware("http", **kwargs)


def test_the_message_names_the_offending_options():
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="name, priority"):
        app.middleware("http", priority=1, name="x")


def test_the_plain_decorator_form_still_works():
    app = Veloce(openapi_url=None)

    @app.middleware("http")
    async def add_header(request, call_next):
        response = await call_next(request)
        response.headers["X-Custom"] = "value"
        return response

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert TestClient(app).get("/x").headers["X-Custom"] == "value"


def test_the_class_form_still_accepts_its_options():
    """The options are real; only the decorator branch could not honour them."""

    class Tracer(Middleware):
        async def process_response(self, request, response):
            response.headers["X-Traced"] = "1"
            return response

    app = Veloce(openapi_url=None)
    app.middleware(Tracer, name="tracer")

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert TestClient(app).get("/x").headers["X-Traced"] == "1"


def test_a_bare_string_that_is_not_http_still_raises():
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="decorator form"):
        app.middleware("https")
