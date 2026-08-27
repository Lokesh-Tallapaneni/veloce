"""The calling principal, per-tool scopes, and OAuth resource-server auth.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import orjson

from tests._mcp_shared import (
    _auth,
    _call,
    _get_prompt,
    _mcp_call_body,
    _read_resource,
    _verify,
)
from veloce import (
    Principal,
    Veloce,
    current_principal,
    set_principal,
)
from veloce.contrib.mcp import MCPAuth

# -- Principal + per-tool scopes --------------------------------------


def test_principal_has_scopes():
    p = Principal(subject="u1", scopes=frozenset({"a", "b"}))
    assert p.has_scope("a")
    assert p.has_scopes(["a", "b"])
    assert not p.has_scopes(["a", "c"])


def test_scoped_tool_rejected_without_principal():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Privileged", scopes=["admin"])
    async def wipe() -> str:
        return "wiped"

    # No principal set (unauthenticated): a scoped tool cannot be satisfied.
    out = _call(app, "wipe", {})
    assert out["error"]["code"] == -32003
    assert "insufficient_scope" in out["error"]["message"]
    assert out["error"]["data"]["requiredScopes"] == ["admin"]


def test_scoped_tool_rejected_with_insufficient_scope():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Privileged", scopes=["admin"])
    async def wipe() -> str:
        return "wiped"

    set_principal(Principal(subject="u1", scopes=frozenset({"read"})))
    out = _call(app, "wipe", {})
    assert out["error"]["code"] == -32003
    assert "insufficient_scope" in out["error"]["message"]


def test_scoped_tool_allowed_with_scope():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Privileged", scopes=["admin"])
    async def wipe() -> str:
        return "wiped"

    set_principal(Principal(subject="u1", scopes=frozenset({"admin", "read"})))
    result = _call(app, "wipe", {})["result"]
    assert result.get("isError") is not True
    assert result["content"][0]["text"] == "wiped"


def test_scoped_resource_forbidden_without_scope():
    app = Veloce(openapi_url=None)

    @app.get(
        "/secret",
        expose_as_mcp_resource=True,
        mcp_resource_uri="secret://data",
        mcp_description="Secret data",
        mcp_scopes=["secrets:read"],
    )
    async def secret() -> dict:
        return {"value": 1}

    set_principal(Principal(scopes=frozenset({"other"})))
    out = _read_resource(app, "secret://data")
    assert out["error"]["code"] == -32003
    assert "insufficient_scope" in out["error"]["message"]


def test_scoped_prompt_forbidden_without_scope():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Privileged prompt", scopes=["prompts:use"])
    async def secret() -> str:
        return "secret"

    set_principal(Principal(scopes=frozenset()))
    out = _get_prompt(app, "secret")
    assert out["error"]["code"] == -32003


def test_tool_reads_current_principal():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Echo the caller subject")
    async def whoami() -> str:
        p = current_principal()
        return p.subject if p else "anon"

    set_principal(Principal(subject="alice"))
    assert _call(app, "whoami", {})["result"]["content"][0]["text"] == "alice"


def test_request_is_mcp_true_over_mcp():
    app = Veloce(openapi_url=None)

    from veloce import Request

    @app.get("/probe", expose_as_mcp_tool=True, mcp_description="Probe")
    async def probe(request: Request) -> dict:
        return {"is_mcp": request.is_mcp}

    # Over MCP the replayed request is flagged.
    out = _call(app, "probe", {})
    assert orjson.loads(out["result"]["content"][0]["text"]) == {"is_mcp": True}
    # Over HTTP it is a real request, not an MCP replay.
    http = app.test_client().get("/probe")
    assert http.json()["is_mcp"] is False


# -- HTTP transport authentication (OAuth Resource Server) ------------


def test_http_auth_missing_token_is_401():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", auth=_auth())
    resp = app.test_client().post("/mcp", json=_mcp_call_body("add", {"a": 1, "b": 2}))
    assert resp.status_code == 401
    assert "Bearer" in resp.headers.get("www-authenticate", "")
    assert "oauth-protected-resource" in resp.headers.get("www-authenticate", "")


def test_http_auth_invalid_token_is_401():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", auth=_auth())
    resp = app.test_client().post(
        "/mcp",
        json=_mcp_call_body("add", {"a": 1, "b": 2}),
        headers={"authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


def test_http_auth_valid_token_dispatches():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", auth=_auth())
    resp = app.test_client().post(
        "/mcp",
        json=_mcp_call_body("add", {"a": 2, "b": 3}),
        headers={"authorization": "Bearer good"},
    )
    assert resp.status_code == 200
    assert orjson.loads(resp.body)["result"]["content"][0]["text"] == "5"


def test_http_auth_endpoint_scope_is_403():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    # Endpoint requires mcp:tools; the 'noscope' principal lacks it.
    app.mount_mcp(transport="http", auth=_auth(required_scopes=["mcp:tools"]))
    resp = app.test_client().post(
        "/mcp",
        json=_mcp_call_body("add", {"a": 1, "b": 2}),
        headers={"authorization": "Bearer noscope"},
    )
    assert resp.status_code == 403
    assert "insufficient_scope" in resp.headers.get("www-authenticate", "")


def test_http_auth_principal_visible_to_tool():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Echo subject")
    async def whoami() -> str:
        p = current_principal()
        return p.subject if p else "anon"

    app.mount_mcp(transport="http", auth=_auth())
    resp = app.test_client().post(
        "/mcp", json=_mcp_call_body("whoami"), headers={"authorization": "Bearer good"}
    )
    assert orjson.loads(resp.body)["result"]["content"][0]["text"] == "agent-1"


def test_http_auth_per_tool_scope_uses_token_scopes():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Privileged", scopes=["admin"])
    async def wipe() -> str:
        return "wiped"

    # Token grants mcp:tools but not admin, so the per-tool scope check rejects
    # with an HTTP 403 + insufficient_scope challenge.
    app.mount_mcp(transport="http", auth=_auth())
    resp = app.test_client().post(
        "/mcp", json=_mcp_call_body("wipe"), headers={"authorization": "Bearer good"}
    )
    assert resp.status_code == 403
    assert "insufficient_scope" in resp.headers.get("www-authenticate", "")
    assert orjson.loads(resp.body)["error"]["code"] == -32003


def test_http_protected_resource_metadata_served():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(
        transport="http",
        auth=MCPAuth(
            verify=_verify,
            resource_server_url="https://api.example.com/mcp",
            authorization_servers=["https://auth.example.com"],
            scopes_supported=["mcp:tools"],
        ),
    )
    resp = app.test_client().get("/.well-known/oauth-protected-resource")
    doc = orjson.loads(resp.body)
    assert doc["resource"] == "https://api.example.com/mcp"
    assert doc["authorization_servers"] == ["https://auth.example.com"]
    assert doc["scopes_supported"] == ["mcp:tools"]
    # RFC 9728 section 2: the client is told how to present the token. Only the
    # header form is read, and the MCP spec forbids the query-string form.
    assert doc["bearer_methods_supported"] == ["header"]


def test_http_query_string_token_is_not_accepted():
    """The advertised bearer method is the only one honoured."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Echo subject")
    async def whoami() -> str:
        principal = current_principal()
        return principal.subject if principal else "anon"

    app.mount_mcp(transport="http", auth=_auth())
    response = app.test_client().post(
        "/mcp?access_token=ok",
        json=_mcp_call_body("whoami", {}),
    )
    assert response.status_code == 401


def test_http_auth_async_verifier():
    app = Veloce(openapi_url=None)

    async def averify(token: str):
        return Principal(subject="async-agent") if token == "ok" else None

    @app.mcp_tool(description="Echo subject")
    async def whoami() -> str:
        p = current_principal()
        return p.subject if p else "anon"

    app.mount_mcp(transport="http", auth=_auth(verify=averify))
    resp = app.test_client().post(
        "/mcp", json=_mcp_call_body("whoami"), headers={"authorization": "Bearer ok"}
    )
    assert orjson.loads(resp.body)["result"]["content"][0]["text"] == "async-agent"
