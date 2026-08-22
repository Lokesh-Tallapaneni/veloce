"""Delivering the error a handler deliberately raised.

`errors.py` tells the author that a tool, resource or prompt handler may raise
`InvalidParamsError` "and the other concrete subclasses" to surface a typed
JSON-RPC error. On `tools/call` only two of them survived: everything else fell
into the generic handler, where the author's own message was redacted to "the
tool raised an internal error" and any `data` payload was discarded.

The split is not which class was listed but what kind of failure it describes.
An error the author raised to say something about the protocol carries a code
and a message they wrote, and goes to the caller as both. An execution failure -
a bad argument, a client capability that is not there - is reported in-band so
the model can read it and adapt. The `_InBandError` subtree marks the second
kind, so the taxonomy answers the question rather than a list of classes.
"""

from __future__ import annotations

from veloce import MCPContext, Veloce
from veloce.contrib.mcp._helpers import _requester_var
from veloce.contrib.mcp.errors import (
    AuthorizationError,
    InternalError,
    InvalidParamsError,
    MCPCapabilityError,
    MCPError,
    ResourceNotFoundError,
    _InBandError,
)
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession

ELICITATION_REQUIRED = -32042


class URLElicitationRequiredError(MCPError):
    """The spec's own retry signal: the call cannot proceed until a URL flow completes."""

    code = ELICITATION_REQUIRED


async def _call(app: Veloce, name: str, arguments: dict | None = None) -> dict:
    return await MCPServer(app).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        MCPSession(),
    )


def _app() -> Veloce:
    app = Veloce(title="Typed", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Raises an invalid-params error")
    async def bad_params() -> str:
        raise InvalidParamsError("the cursor is not one this server issued")

    @app.mcp_tool(description="Raises a not-found error")
    async def missing() -> str:
        raise ResourceNotFoundError("no such ledger")

    @app.mcp_tool(description="Raises an internal error with its own message")
    async def broken() -> str:
        raise InternalError("the ledger service is down")

    @app.mcp_tool(description="Needs an out-of-band authorization first")
    async def needs_elicitation() -> str:
        raise URLElicitationRequiredError(
            "This request requires more information.",
            data={"elicitations": [{"mode": "url", "url": "https://example.com/connect"}]},
        )

    @app.mcp_tool(description="Raises an ordinary programming error")
    async def crashes() -> str:
        raise ValueError("division of the accounts ledger by zero")

    @app.mcp_tool(description="Takes a number")
    async def counted(count: int) -> int:
        return count

    @app.mcp_tool(description="Samples against a client that cannot sample")
    async def summarise(ctx: MCPContext) -> str:
        await ctx.sample([{"role": "user", "content": {"type": "text", "text": "x"}}], max_tokens=8)
        return "never reached"

    @app.mcp_tool(description="Reads a scoped resource it may not read")
    async def forbidden(ctx: MCPContext) -> str:
        raise AuthorizationError(frozenset({"admin"}))

    return app


# ── A deliberately raised error reaches the caller ───────────────────


async def test_an_invalid_params_error_keeps_its_code():
    error = (await _call(_app(), "bad_params"))["error"]
    assert error["code"] == -32602
    assert error["message"] == "the cursor is not one this server issued"


async def test_a_not_found_error_keeps_its_code():
    """It used to arrive as `-32603 internal error` with the message gone."""
    error = (await _call(_app(), "missing"))["error"]
    assert (error["code"], error["message"]) == (-32002, "no such ledger")


async def test_an_internal_error_keeps_the_authors_message():
    """Redaction is for what the author did not write."""
    error = (await _call(_app(), "broken"))["error"]
    assert (error["code"], error["message"]) == (-32603, "the ledger service is down")


async def test_a_custom_code_and_its_data_payload_both_survive():
    """The spec's URL-elicitation retry needs exactly this, from `tools/call`."""
    error = (await _call(_app(), "needs_elicitation"))["error"]
    assert error["code"] == ELICITATION_REQUIRED
    assert error["data"]["elicitations"][0]["url"] == "https://example.com/connect"


async def test_an_authorization_failure_still_reaches_the_caller():
    app = Veloce(title="Scoped", openapi_url=None)

    @app.mcp_tool(description="Reads a scoped resource through its context")
    async def reader(ctx: MCPContext) -> str:
        raise AuthorizationError(frozenset({"admin"}))

    assert (await _call(app, "reader"))["error"]["code"] == -32003


async def test_a_route_backed_tool_delivers_its_error_too():
    """Its exception used to be rendered by the app's handlers as an HTTP body."""
    app = Veloce(title="Routed", openapi_url=None)

    @app.get("/connect", expose_as_mcp_tool=True, mcp_description="Connect the account")
    async def connect() -> dict:
        raise URLElicitationRequiredError("authorize first", data={"elicitations": []})

    response = await _call(app, "connect")
    assert "result" not in response
    assert response["error"]["code"] == ELICITATION_REQUIRED
    assert response["error"]["message"] == "authorize first"


# ── An execution failure is still reported in-band ───────────────────


async def test_an_ordinary_exception_is_still_redacted_in_band():
    """Nothing about a `ValueError` was written for the model to read."""
    result = (await _call(_app(), "crashes"))["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "the tool raised an internal error"
    assert "ledger" not in result["content"][0]["text"]


async def test_a_bad_argument_is_still_reported_in_band_verbatim():
    result = (await _call(_app(), "counted", {"count": []}))["result"]
    assert result["isError"] is True
    assert "count" in result["content"][0]["text"]


async def test_an_unavailable_client_capability_is_reported_in_band():
    """The caller's request was fine; what failed is what the tool tried to do."""

    async def never_asked(method: str, params: dict) -> dict:
        raise AssertionError("the request must not reach the wire")

    token = _requester_var.set(never_asked)
    try:
        result = (await _call(_app(), "summarise"))["result"]
    finally:
        _requester_var.reset(token)
    assert result["isError"] is True
    assert "sampling" in result["content"][0]["text"]


def test_the_capability_error_is_marked_as_an_execution_failure():
    """The taxonomy carries the decision, so every catch site agrees."""
    assert issubclass(MCPCapabilityError, _InBandError)
    assert issubclass(MCPCapabilityError, MCPError)


# ── The other doors are unchanged ────────────────────────────────────


async def test_a_prompt_handler_still_delivers_its_error():
    app = Veloce(title="Prompted", openapi_url=None)

    @app.mcp_prompt(description="Needs authorization first")
    async def brief() -> str:
        raise URLElicitationRequiredError("authorize first")

    response = await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "prompts/get", "params": {"name": "brief"}},
        MCPSession(),
    )
    assert response["error"]["code"] == ELICITATION_REQUIRED


async def test_a_resource_read_still_delivers_its_error():
    app = Veloce(title="Resourced", openapi_url=None)

    @app.get(
        "/ledger",
        expose_as_mcp_resource=True,
        mcp_resource_uri="doc://ledger",
        mcp_description="The ledger",
    )
    async def ledger() -> dict:
        raise InvalidParamsError("the ledger is not addressable that way")

    response = await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "doc://ledger"}},
        MCPSession(),
    )
    assert response["error"]["code"] == -32602


# ── What instrumentation records ─────────────────────────────────────


async def test_each_failure_is_recorded_under_a_status_that_describes_it():
    app = _app()
    seen: list[tuple[str | None, int]] = []

    @app.add_instrumentation
    def record(metrics) -> None:
        seen.append((metrics.route, metrics.status_code))

    async def never_asked(method: str, params: dict) -> dict:
        raise AssertionError("the request must not reach the wire")

    server = MCPServer(app)
    token = _requester_var.set(never_asked)
    try:
        for name, arguments in (
            ("missing", {}),
            ("bad_params", {}),
            ("crashes", {}),
            ("counted", {"count": []}),
            ("summarise", {}),
            ("forbidden", {}),
        ):
            await server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                MCPSession(),
            )
    finally:
        _requester_var.reset(token)

    recorded = dict(seen)
    assert recorded["missing"] == 404
    assert recorded["bad_params"] == 422
    assert recorded["crashes"] == 500
    assert recorded["counted"] == 422
    assert recorded["summarise"] == 424
    assert recorded["forbidden"] == 403
