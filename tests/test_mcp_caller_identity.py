"""The caller's identity survives the replay of a route as an MCP tool.

A route exposed as a tool is invoked by replaying its request lifecycle against
a synthetic `Request`. That request was built with no transport peer, so
`request.client_host` was `None` on every tool call while the carrying
`POST /mcp` knew exactly who was calling.

Anything keyed on caller identity was therefore blind on the agent-facing door.
Rate limiting is the sharpest case, because it fails open in silence:

    @app.get("/costly", expose_as_mcp_tool=True, mcp_description="...")
    @rate_limit(FixedWindow(limit=2, window=60))
    async def costly() -> dict: ...

    over HTTP        ->  200, 200, 429, 429
    over tools/call  ->  served, served, served, served

`RateLimitMiddleware._bucket_key` falls through peer -> `X-Forwarded-For` ->
`User-Agent` -> a fresh UUID per request. That last step is the correct failure
mode for genuinely anonymous HTTP traffic (fail open per caller, never shared
across callers), but a replayed MCP request hit it every time, so each call got
a brand-new bucket and no counter ever accumulated. `docs/guide/mcp.md` promises
the opposite: an exposed route keeps every guard it has as an HTTP endpoint.

The fix carries the carrier request's *resolved* `client_host` onto the
synthetic request, so the replay reports the same caller the transport saw -
including a `ProxyFix` correction, since the carrier's property already applied
it. Parity with the HTTP path is the invariant; no header is copied.

That last point is deliberate. Headers stay empty on a replayed request so a
tool argument cannot masquerade as transport-authenticated input, and so a
credential presented to the transport is not re-read by a route's own
`Security` scheme. Both properties are pinned below, because the fix would be a
security regression if it widened into copying headers.

Without a carrier at all - a stdio server - there is nothing to inherit, so
`client_host` stays `None` and limiting keeps failing open per call.
"""

from __future__ import annotations

from veloce import Middleware, Veloce, rate_limit
from veloce.middleware.security import RateLimitMiddleware
from veloce.ratelimit import FixedWindow
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


def _client(app: Veloce) -> TestClient:
    client = TestClient(app)
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    return client


def _call(client: TestClient, tool: str, ident: int, **headers: str):
    body = {
        "jsonrpc": "2.0",
        "id": ident,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {}},
    }
    return client.post("/mcp", json=body, headers={"Accept": "application/json", **headers})


def _served(response) -> bool:
    payload = response.json()
    return "result" in payload and not payload["result"].get("isError")


def _limited_app(limit: int = 2) -> Veloce:
    app = Veloce(title="T", version="1", openapi_url=None)
    app.add_middleware(RateLimitMiddleware(strategy=FixedWindow(limit=1000, window=60)))

    @app.get("/costly", expose_as_mcp_tool=True, mcp_description="expensive")
    @rate_limit(FixedWindow(limit=limit, window=60))
    async def costly() -> dict:
        return {"ok": True}

    app.mount_mcp(transport="http", path="/mcp")
    return app


# ── the defect ───────────────────────────────────────────────────────


def test_a_route_rate_limit_applies_to_its_tool_call():
    """The defect: every tool call got a fresh bucket, so nothing accumulated."""
    client = _client(_limited_app(limit=2))
    verdicts = [_served(_call(client, "costly", i)) for i in range(1, 5)]
    assert verdicts == [True, True, False, False]


def test_the_tool_call_is_limited_the_same_way_the_http_call_is():
    """Parity is the invariant, so compare the two doors directly."""
    http = _client(_limited_app(limit=2))
    over_http = [http.get("/costly").status_code == 200 for _ in range(4)]
    mcp = _client(_limited_app(limit=2))
    over_mcp = [_served(_call(mcp, "costly", i)) for i in range(1, 5)]
    assert over_http == over_mcp


def test_both_doors_share_one_budget():
    """One handler, one limit - spending it over HTTP must spend it for the agent."""
    client = _client(_limited_app(limit=2))
    assert client.get("/costly").status_code == 200
    assert client.get("/costly").status_code == 200
    assert not _served(_call(client, "costly", 1))


def test_the_replayed_request_reports_the_carrier_client():
    seen: list[tuple[str, str | None]] = []

    class Probe(Middleware):
        async def process_request(self, request):
            seen.append((request.path, request.client_host))
            return None

    app = Veloce(title="T", version="1", openapi_url=None)
    app.add_middleware(Probe())

    @app.get("/who", expose_as_mcp_tool=True, mcp_description="who")
    async def who() -> dict:
        return {}

    app.mount_mcp(transport="http", path="/mcp")
    client = _client(app)
    seen.clear()
    _call(client, "who", 1)
    observed = dict(seen)
    assert observed["/mcp"] is not None
    assert observed["/who"] == observed["/mcp"]


def test_a_native_tool_call_also_carries_the_caller():
    """A tool with no route builds the same request and must not be exempt.

    Asserted through the injected `Request` rather than through middleware: a
    native tool has no route, so no request middleware is replayed around it.
    """
    from veloce import Request

    app = Veloce(title="T", version="1", openapi_url=None)

    @app.mcp_tool(description="native")
    async def native(request: Request) -> str:
        return str(request.client_host)

    app.mount_mcp(transport="http", path="/mcp")
    client = _client(app)
    text = _call(client, "native", 1).json()["result"]["content"][0]["text"]
    assert text not in ("None", "")


def test_a_native_tool_is_not_wrapped_in_request_middleware():
    """Pins why the test above reads the request directly - no route, no replay."""
    seen: list[str] = []

    app = Veloce(title="T", version="1", openapi_url=None)

    @app.mcp_tool(description="native")
    async def native() -> str:
        return "ok"

    class Probe(Middleware):
        async def process_request(self, request):
            seen.append(request.path)
            return None

    app.add_middleware(Probe())
    app.mount_mcp(transport="http", path="/mcp")
    client = _client(app)
    seen.clear()
    _call(client, "native", 1)
    assert seen == ["/mcp"]


def test_the_handler_is_not_invoked_once_the_limit_is_reached():
    """A limit that lets the work happen anyway is not a limit."""
    runs: list[int] = []
    app = Veloce(title="T", version="1", openapi_url=None)
    app.add_middleware(RateLimitMiddleware(strategy=FixedWindow(limit=1000, window=60)))

    @app.get("/work", expose_as_mcp_tool=True, mcp_description="work")
    @rate_limit(FixedWindow(limit=1, window=60))
    async def work() -> dict:
        runs.append(1)
        return {"ok": True}

    app.mount_mcp(transport="http", path="/mcp")
    client = _client(app)
    for i in range(1, 4):
        _call(client, "work", i)
    assert len(runs) == 1


def test_the_limit_surfaces_to_the_agent_as_an_error():
    """A refused call must read as an error, not as a successful empty result."""
    client = _client(_limited_app(limit=1))
    _call(client, "costly", 1)
    payload = _call(client, "costly", 2).json()
    assert payload["result"]["isError"] is True


# ── the security properties the fix must not widen ───────────────────


def test_no_carrier_header_reaches_the_replayed_request():
    """Headers stay empty: a transport credential is not re-read by the route."""
    seen: dict[str, object] = {}

    class Probe(Middleware):
        async def process_request(self, request):
            if request.path == "/guarded":
                seen["authorization"] = request.headers.get("authorization")
                seen["cookie"] = request.headers.get("cookie")
                seen["count"] = len(request.headers)
            return None

    app = Veloce(title="T", version="1", openapi_url=None)
    app.add_middleware(Probe())

    @app.get("/guarded", expose_as_mcp_tool=True, mcp_description="guarded")
    async def guarded() -> dict:
        return {}

    app.mount_mcp(transport="http", path="/mcp")
    client = _client(app)
    _call(client, "guarded", 1, Authorization="Bearer carrier-secret", Cookie="sid=abc")
    assert seen["authorization"] is None
    assert seen["cookie"] is None
    assert seen["count"] == 0


def test_a_tool_argument_still_cannot_forge_a_header():
    """The pre-existing rule: arguments feed query/form, never headers/cookies."""
    seen: dict[str, object] = {}

    class Probe(Middleware):
        async def process_request(self, request):
            if request.path == "/echo":
                seen["x_api_key"] = request.headers.get("x-api-key")
            return None

    app = Veloce(title="T", version="1", openapi_url=None)
    app.add_middleware(Probe())

    @app.get("/echo", expose_as_mcp_tool=True, mcp_description="echo")
    async def echo(x_api_key: str = "none") -> dict:
        return {"seen": x_api_key}

    app.mount_mcp(transport="http", path="/mcp")
    client = _client(app)
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"x-api-key": "forged"}},
        },
        headers={"Accept": "application/json"},
    )
    assert seen["x_api_key"] is None


def test_a_replay_without_a_carrier_still_serves():
    """A stdio server has no carrier: nothing to inherit, and no crash."""
    from veloce.contrib.mcp.plan_bridge import _build_request

    request = _build_request("tool", {"a": 1})
    assert request.client_host is None
    assert len(request.headers) == 0
