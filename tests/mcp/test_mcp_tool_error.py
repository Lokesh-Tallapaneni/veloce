"""A tool author's own way to report that the work failed.

MCP keeps two failures apart. A *protocol* error travels as a JSON-RPC `error`
object and is not shown to the language model; a *tool execution* error travels
on the tool result as `isError: true` and is. Veloce modelled both, but every
error class a tool author could reach took the first channel: the in-band
subtree was private, so "that record is locked, retry shortly" could only be
said on the channel the spec reserves for protocol faults, where the model never
reads it.

`ToolError` is the in-band subtree's public member. The two channels stay
distinct, which is the point: these tests assert each failure kind still leaves
by its own door.
"""

from __future__ import annotations

import veloce.contrib.mcp as mcp
from tests._mcp import INTERNAL_ERROR, call_raw, call_tool
from veloce import MCPContext, Response, StreamingResponse, TestClient, Veloce
from veloce.contrib.mcp import InvalidParamsError, MCPError, ToolError
from veloce.contrib.mcp._helpers import _requester_var
from veloce.contrib.mcp.errors import (
    _InBandError,
    _InvalidArgumentsError,
    _StreamTimeoutError,
    _StreamTooLargeError,
)
from veloce.contrib.mcp.server import MCPServer, _in_band_status_for
from veloce.contrib.mcp.session import MCPSession

LOCKED = "Record 42 is locked by another session; retry shortly."


def _app() -> Veloce:
    app = Veloce(title="Archive", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Archive a record")
    async def archive(record_id: str) -> dict:
        raise ToolError(LOCKED)

    @app.mcp_tool(description="Archive a record, attaching a payload")
    async def archive_with_data(record_id: str) -> dict:
        raise ToolError(LOCKED, data={"retryAfter": 30})

    @app.mcp_tool(description="Raises a protocol-level error instead")
    async def protocol_fault() -> dict:
        raise InvalidParamsError("the cursor is not one this server issued")

    @app.mcp_tool(description="Raises an unexpected programming error")
    async def crashes() -> dict:
        raise RuntimeError("the ledger password is hunter2")

    @app.mcp_tool(description="Takes a number")
    async def counted(count: int) -> int:
        return count

    @app.mcp_tool(description="Refuses partway through a streamed body")
    async def archive_streaming() -> StreamingResponse:
        async def chunks():
            yield b"partial "
            raise ToolError(LOCKED)

        return StreamingResponse(chunks())

    @app.mcp_tool(description="Samples against a client that cannot sample")
    async def summarise(ctx: MCPContext) -> str:
        await ctx.sample([{"role": "user", "content": {"type": "text", "text": "x"}}], max_tokens=8)
        return "never reached"

    return app


async def _call_envelope(app: Veloce, name: str) -> dict:
    envelope = await call_raw(
        MCPServer(app), "tools/call", {"name": name, "arguments": {"record_id": "42"}}
    )
    assert envelope is not None
    return envelope


# ── the author's message reaches the agent ───────────────────────────


async def test_a_tool_error_is_reported_in_band():
    result = await call_tool(_app(), "archive", {"record_id": "42"})
    assert result["isError"] is True


async def test_the_authors_message_reaches_the_caller_verbatim():
    """The whole point: a redacted message is one the model cannot act on."""
    result = await call_tool(_app(), "archive", {"record_id": "42"})
    assert result["content"][0]["text"] == LOCKED


async def test_a_tool_error_is_not_sent_on_the_jsonrpc_error_channel():
    envelope = await _call_envelope(_app(), "archive")
    assert "error" not in envelope
    assert envelope["result"]["isError"] is True


async def test_a_tool_error_carries_no_data_onto_the_result():
    """`data=` has nowhere to go in-band; the message is the whole payload."""
    result = await call_tool(_app(), "archive_with_data", {"record_id": "42"})
    assert result["content"] == [{"type": "text", "text": LOCKED}]


async def test_a_route_backed_tool_reports_it_in_band_too():
    """A route-backed tool's exceptions go through the app's handlers; not this one."""
    app = Veloce(title="Routed", openapi_url=None)

    @app.post("/archive", expose_as_mcp_tool=True, mcp_description="Archive a record")
    async def archive() -> dict:
        raise ToolError(LOCKED)

    result = await call_tool(app, "archive")
    assert result["isError"] is True
    assert result["content"][0]["text"] == LOCKED


# ── the two channels stay distinct ───────────────────────────────────


async def test_a_protocol_error_still_leaves_by_the_error_channel():
    """Collapsing the two would make `isError` mean "something went wrong"."""
    envelope = await _call_envelope(_app(), "protocol_fault")
    assert "result" not in envelope
    assert envelope["error"]["message"] == "the cursor is not one this server issued"


async def test_an_unexpected_exception_is_still_redacted_in_band():
    """Unchanged: nothing in a `RuntimeError` was written for the model to read."""
    result = await call_tool(_app(), "crashes")
    assert result["isError"] is True
    assert result["content"][0]["text"] == "the tool raised an internal error"
    assert "hunter2" not in result["content"][0]["text"]


async def test_a_tool_error_outside_the_tool_path_takes_the_error_channel():
    """A prompt has no in-band channel, so the failure travels as its code."""
    app = Veloce(title="Prompted", openapi_url=None)

    @app.mcp_prompt(description="Brief the agent")
    async def brief() -> str:
        raise ToolError(LOCKED)

    envelope = await call_raw(MCPServer(app), "prompts/get", {"name": "brief"})
    assert envelope is not None
    assert envelope["error"]["code"] == INTERNAL_ERROR


# ── the public surface ───────────────────────────────────────────────


def test_tool_error_is_importable_from_the_gateway():
    assert mcp.ToolError is ToolError
    assert "ToolError" in mcp.__all__


def test_tool_error_is_the_public_member_of_the_in_band_subtree():
    """The taxonomy carries the routing decision, so every catch site agrees."""
    assert issubclass(ToolError, _InBandError)
    assert issubclass(ToolError, MCPError)


# ── what instrumentation records ─────────────────────────────────────


async def _recorded_statuses() -> dict[str | None, int]:
    """Drive one call per failure kind and return `{tool name: recorded status}`."""
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
            ("archive", {"record_id": "42"}),
            ("counted", {"count": []}),
            ("summarise", {}),
            ("crashes", {}),
            ("archive_streaming", {}),
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
    return dict(seen)


async def test_a_tool_error_is_not_recorded_as_a_server_fault():
    """The defect: it fell through to 500, so expected tool failures fired 5xx alerts."""
    assert (await _recorded_statuses())["archive"] == 409


async def test_a_bad_argument_still_records_unprocessable():
    assert (await _recorded_statuses())["counted"] == 422


async def test_an_unavailable_client_capability_still_records_failed_dependency():
    assert (await _recorded_statuses())["summarise"] == 424


async def test_an_unexpected_exception_still_records_a_server_fault():
    """This one really is a server fault, and must stay in the 5xx rate."""
    assert (await _recorded_statuses())["crashes"] == 500


def test_a_stream_drain_failure_still_records_a_server_fault():
    """The fallthrough is for the server's own limits, which `ToolError` is not."""
    assert _in_band_status_for(_StreamTooLargeError("too big")) == 500
    assert _in_band_status_for(_StreamTimeoutError("too slow")) == 500
    assert _in_band_status_for(_InvalidArgumentsError("bad")) == 422
    assert _in_band_status_for(ToolError(LOCKED)) == 409


# ── the mapping has to be applied, not just defined ──────────────────
#
# Asserting `_in_band_status_for` in isolation says the table is right; it says
# nothing about whether each raising path consults it. The drain paths did not:
# they answered 500 directly, so the same refusal was a 409 when it was raised
# before the body streamed and a server fault when it was raised during.


async def test_a_tool_error_raised_while_streaming_records_the_same_status():
    assert (await _recorded_statuses())["archive_streaming"] == 409


async def test_a_tool_error_records_one_status_whichever_path_raised_it():
    """The property the two paths have to share, stated as one assertion."""
    recorded = await _recorded_statuses()
    assert recorded["archive_streaming"] == recorded["archive"]


async def test_a_tool_error_raised_while_streaming_is_still_reported_in_band():
    """The negative: the status fix must not move the failure off its channel."""
    result = await call_tool(_app(), "archive_streaming", {})
    assert result["isError"] is True
    assert LOCKED in result["content"][0]["text"]


# ── the same refusal, answered on the HTTP door ──────────────────────
#
# A route-backed tool is one handler behind two doors, and it raises one
# `ToolError` for both. The agent door surfaces the author's message verbatim;
# the HTTP door had no answer for the class at all, so the refusal arrived as
# `500 Internal Server Error` with the message dropped and a traceback logged -
# a deliberate, caller-fixable refusal reported as a server fault.


def _route_backed_app() -> Veloce:
    app = Veloce(title="Archive", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Archive a record")
    @app.get("/archive")
    async def archive() -> dict:
        raise ToolError(LOCKED)

    return app


def test_a_tool_error_is_a_conflict_on_the_http_door():
    with TestClient(_route_backed_app()) as client:
        response = client.get("/archive")
    assert response.status_code == 409
    assert response.json()["detail"] == LOCKED


async def test_the_http_answer_does_not_change_the_agent_answer():
    """The in-band channel is the point of `ToolError`; it must stay in band."""
    result = await call_tool(_route_backed_app(), "archive", {})
    assert result["isError"] is True
    assert result["content"][0]["text"] == LOCKED


async def test_a_pure_tool_still_reports_in_band():
    """The control: registering the HTTP mapping must not disturb a routeless tool."""
    result = await call_tool(_app(), "archive", {"record_id": "42"})
    assert result["isError"] is True
    assert result["content"][0]["text"] == LOCKED


def test_an_app_that_maps_tool_error_itself_keeps_its_own_answer():
    """The mapping is a default, not an override."""
    app = Veloce(title="Archive", version="1.0.0", openapi_url=None)

    @app.errorhandler(ToolError)
    async def mine(request, exc) -> Response:
        return Response(status_code=418, body=b"mine", content_type="text/plain")

    @app.mcp_tool(description="Archive a record")
    @app.get("/archive")
    async def archive() -> dict:
        raise ToolError(LOCKED)

    with TestClient(app) as client:
        response = client.get("/archive")
    assert response.status_code == 418
    assert response.body == b"mine"


def test_an_unexpected_exception_is_still_a_server_fault_on_the_http_door():
    """The negative: only `ToolError` is a stated refusal."""
    app = Veloce(title="Archive", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Crashes")
    @app.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("the ledger password is hunter2")

    with TestClient(app) as client:
        response = client.get("/boom")
    assert response.status_code == 500
    assert "hunter2" not in response.text
