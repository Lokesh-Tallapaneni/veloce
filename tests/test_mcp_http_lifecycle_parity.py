"""The MCP door runs a route's lifecycle in the same order HTTP dispatch does.

`contrib/mcp/_invocation.py` replays the dispatch lifecycle by calling
`DispatchMixin`'s private methods in sequence, with comments that read "exactly
as `_dispatch_request` does". Nothing checked that claim. A reordering in
`_dispatch_request` would leave the MCP door silently on the old order, and the
same seam produced three separate Tier-1 defects - a security scheme invisible to
one door, `response_model` filtering that ran on one and not the other, and a
sub-dependency parameter published by one and not the other.

**Why the two are not merged instead.** They could be, and it would be the
tidier answer, but `_dispatch_request` is the per-request hot path and this
repository has a recorded measurement for exactly that move: extracting its
inline stages into shared async helpers cost ~5% per request, because each
extracted stage adds a coroutine await to every request. So the seam stays, and
this file is what makes it safe - the divergence is now detectable rather than
structurally unpreventable.

**How.** Every observable stage of the lifecycle appends its name to one list.
The same route is then driven once over HTTP and once over MCP, and the two
sequences are compared. This is behavioural rather than source-inspecting: it
sees the order the stages actually run in, not the order they appear in the
file.

**The deliberate differences**, each with its reason, are listed in
`_MCP_OMITS` below. A stage that stops matching for any *other* reason fails
here.
"""

from __future__ import annotations

import pytest

from veloce import Depends, Middleware, Response, Veloce, request
from veloce.testclient import TestClient

pytest.importorskip("veloce.contrib.mcp")

#: Stages the MCP door deliberately does not run, and why. Each is asserted
#: individually below, so this list cannot rot into a way to excuse a new gap.
_MCP_OMITS = {
    # The tool result is derived from the response *body*, not from a wire
    # response, so a response-mutating middleware (compression, security
    # headers) has nothing to act on.
    "middleware:response",
}


def _record(events, name):
    """Record a stage, but only for the route under test.

    A `tools/call` arrives as an HTTP POST to `/mcp`, so that request runs the
    app's own lifecycle before the MCP door replays the tool route's. Both reach
    this recorder; only the inner one is the subject.
    """
    try:
        path = request.path
    except Exception:
        path = ""
    if path.startswith("/mcp"):
        return
    events.append(name)


def _build(events):
    """One app whose every lifecycle stage records itself."""
    app = Veloce(openapi_url=None)

    class Recorder(Middleware):
        async def process_request(self, request):
            _record(events, "middleware:request")
            return None

        async def process_response(self, request, response):
            _record(events, "middleware:response")
            return response

    app.add_middleware(Recorder())

    @app.before_request
    async def before(request):
        _record(events, "before_request")

    @app.after_request
    async def after(request, response):
        _record(events, "after_request")
        return response

    @app.teardown_request
    async def teardown(exc):
        _record(events, "teardown_request")

    @app.url_value_preprocessor
    def preprocess(endpoint, values):
        _record(events, "url_value_preprocessor")

    async def dependency():
        _record(events, "dependency")
        return "d"

    @app.get(
        "/thing/{thing_id}",
        expose_as_mcp_tool=True,
        mcp_description="A thing",
    )
    async def thing(thing_id: str, dep: str = Depends(dependency)) -> dict:
        _record(events, "handler")
        return {"thing_id": thing_id, "dep": dep}

    return app


def _over_http(app, events, path="/thing/abc"):
    events.clear()
    TestClient(app).get(path)
    return list(events)


def _rpc(client, method, params, request_id):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        headers={"Accept": "application/json"},
    ).json()


def _over_mcp(app, events, arguments=None):
    """Drive the app's single MCP tool and return the stages it ran.

    The tool name is read back from `tools/list` rather than assumed, so the
    test does not depend on how a route's tool name is derived.
    """
    client = TestClient(app)
    _rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "p", "version": "1"},
        },
        0,
    )
    listing = _rpc(client, "tools/list", {}, 1)
    tools = listing["result"]["tools"]
    assert len(tools) == 1, tools
    name = tools[0]["name"]
    events.clear()
    result = _rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, 2)
    # A transport-level error means the call never reached the lifecycle, which
    # would make an empty stage list look like agreement.
    assert "error" not in result, result
    return list(events)


@pytest.fixture
def orders():
    events: list[str] = []
    app = _build(events)
    app.mount_mcp(transport="http", path="/mcp")
    return (
        _over_http(app, events, "/thing/abc"),
        _over_mcp(app, events, {"thing_id": "abc"}),
    )


# ── the orders agree ─────────────────────────────────────────────────


def test_the_two_doors_run_the_same_stages_in_the_same_order(orders):
    """The property the whole finding is about."""
    http, mcp = orders
    assert [stage for stage in http if stage not in _MCP_OMITS] == mcp


def test_the_http_door_runs_every_stage(orders):
    """A guard on the guard: if the recorder stopped recording, the comparison
    above would pass vacuously."""
    http, _ = orders
    assert set(http) == {
        "middleware:request",
        "before_request",
        "url_value_preprocessor",
        "dependency",
        "handler",
        "after_request",
        "middleware:response",
        "teardown_request",
    }


def test_the_mcp_door_runs_every_stage_it_should(orders):
    _, mcp = orders
    assert set(mcp) == {
        "middleware:request",
        "before_request",
        "url_value_preprocessor",
        "dependency",
        "handler",
        "after_request",
        "teardown_request",
    }


@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        ("middleware:request", "before_request"),
        ("before_request", "url_value_preprocessor"),
        ("url_value_preprocessor", "dependency"),
        ("dependency", "handler"),
        ("handler", "after_request"),
        ("after_request", "teardown_request"),
    ],
)
def test_each_ordering_constraint_holds_on_both_doors(orders, earlier, later):
    """Named pairwise, so a failure says which two stages swapped."""
    for order in orders:
        assert order.index(earlier) < order.index(later), (earlier, later, order)


def test_the_omitted_stage_is_omitted_for_the_stated_reason(orders):
    """`_MCP_OMITS` must describe reality, not excuse a drift."""
    http, mcp = orders
    assert set(http) - set(mcp) == _MCP_OMITS


# ── the same holds when a stage short-circuits ───────────────────────


def _short_circuit_app(events, where):
    app = Veloce(openapi_url=None)

    # Gated on the route under test: a `tools/call` arrives as a POST to
    # `/mcp`, and short-circuiting *that* would stop the request before the MCP
    # door ever replays the tool's lifecycle - which is a different thing from
    # the tool call being short-circuited.
    def _under_test(req):
        return not req.path.startswith("/mcp")

    class Blocker(Middleware):
        async def process_request(self, req):
            _record(events, "middleware:request")
            if where == "middleware" and _under_test(req):
                return Response(body=b"no", status_code=403)
            return None

    app.add_middleware(Blocker())

    @app.before_request
    async def before(request):
        _record(events, "before_request")
        if where == "before_request" and _under_test(request):
            return Response(body=b"no", status_code=403)
        return None

    @app.teardown_request
    async def teardown(exc):
        _record(events, "teardown_request")

    @app.get("/t", expose_as_mcp_tool=True, mcp_description="t")
    async def t() -> dict:
        _record(events, "handler")
        return {}

    app.mount_mcp(transport="http", path="/mcp")
    return app


@pytest.mark.parametrize("where", ["middleware", "before_request"])
def test_a_short_circuit_runs_teardown_on_both_doors(where):
    """A rejected call must still release what it acquired, either way in."""
    events: list[str] = []
    app = _short_circuit_app(events, where)
    http = _over_http(app, events, "/t")
    mcp = _over_mcp(app, events)
    assert "teardown_request" in http
    assert "teardown_request" in mcp


@pytest.mark.parametrize("where", ["middleware", "before_request"])
def test_a_short_circuit_skips_the_handler_on_both_doors(where):
    events: list[str] = []
    app = _short_circuit_app(events, where)
    assert "handler" not in _over_http(app, events, "/t")
    assert "handler" not in _over_mcp(app, events)


@pytest.mark.parametrize("where", ["middleware", "before_request"])
def test_a_short_circuit_stops_at_the_same_stage_on_both_doors(where):
    events: list[str] = []
    app = _short_circuit_app(events, where)
    http = [s for s in _over_http(app, events, "/t") if s not in _MCP_OMITS]
    assert http == _over_mcp(app, events)


# ── and when the handler raises ──────────────────────────────────────


def test_a_raising_handler_runs_teardown_on_both_doors():
    events: list[str] = []
    app = Veloce(openapi_url=None)

    @app.teardown_request
    async def teardown(exc):
        _record(events, f"teardown_request:{exc is not None}")

    @app.get("/boom", expose_as_mcp_tool=True, mcp_description="b")
    async def boom() -> dict:
        raise RuntimeError("boom")

    app.mount_mcp(transport="http", path="/mcp")

    events.clear()
    TestClient(app).get("/boom")
    http = list(events)

    client = TestClient(app)
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "p", "version": "1"},
            },
        },
        headers={"Accept": "application/json"},
    )
    events.clear()
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "boom", "arguments": {}},
        },
        headers={"Accept": "application/json"},
    )
    assert http == events == ["teardown_request:True"]


def test_a_yield_dependency_tears_down_on_both_doors():
    """The resource was acquired before the handler; both doors must release it."""
    events: list[str] = []
    app = Veloce(openapi_url=None)

    async def resource():
        _record(events, "acquire")
        try:
            yield "r"
        finally:
            _record(events, "release")

    @app.get("/r", expose_as_mcp_tool=True, mcp_description="r")
    async def route(res: str = Depends(resource)) -> dict:
        _record(events, "handler")
        return {}

    app.mount_mcp(transport="http", path="/mcp")
    assert _over_http(app, events, "/r") == ["acquire", "handler", "release"]
    assert _over_mcp(app, events) == ["acquire", "handler", "release"]
