"""MCP server-initiated requests - sampling, elicitation, and roots via MCPContext.

The stdio transport is bidirectional: a tool handler may call `ctx.sample`,
`ctx.elicit`, or `ctx.roots` to issue a server->client request and await the
client's correlated reply. These tests drive a `StdioTransport` against a scripted
client that replies to those requests, asserting the wire shape, the reply
plumbing, and the client-capability gating.
"""

from __future__ import annotations

import asyncio

import orjson
import pytest

from veloce import Veloce
from veloce.contrib.mcp import MCPCapabilityError, MCPContext
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.transports.stdio import MCPRequestError, StdioTransport


class _Client:
    """A scripted MCP client over a duplex queue, replying to server requests.

    Feeds an `initialize` carrying `capabilities`, then one `tools/call`. When the
    server issues a request (`sampling/createMessage`, `elicitation/create`,
    `roots/list`) the registered `responder` produces its reply. Every outbound
    server message is recorded for assertions.
    """

    def __init__(
        self,
        server: MCPServer,
        capabilities: dict,
        responder,
    ) -> None:
        self._to_server: asyncio.Queue = asyncio.Queue()
        self._responder = responder
        self.sent: list[dict] = []
        self.requests: list[dict] = []
        self.transport = StdioTransport(server, self._read_line, self._write_line)
        self._capabilities = capabilities
        self._call_id: object = None

    def _enqueue(self, message: dict) -> None:
        self._to_server.put_nowait(orjson.dumps(message))

    async def _read_line(self) -> bytes | None:
        return await self._to_server.get()

    async def _write_line(self, data: bytes) -> None:
        message = orjson.loads(data)
        self.sent.append(message)
        # A server->client request gets a reply pushed back; a one-way notification
        # or a response to the client's own call is just recorded.
        if "method" in message and "id" in message:
            self.requests.append(message)
            reply = self._responder(message)
            self._enqueue({"jsonrpc": "2.0", "id": message["id"], **reply})

    async def run(self, call: dict) -> list[dict]:
        self._enqueue(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"capabilities": self._capabilities},
            }
        )
        self._enqueue(call)
        self._call_id = call.get("id")
        # Close the input once the call has been answered so the serve loop ends.
        asyncio.get_running_loop().call_soon(self._close_when_done)
        await self.transport.serve()
        return self.sent

    def _close_when_done(self) -> None:
        # End-of-input sentinel: a `None` line stops the serve loop. The trigger
        # is the call's own response, not an empty queue: the loop reads each
        # line and dispatches it, so the queue drains while the handler is still
        # running - and closing then would cut off the reply to a server->client
        # request the handler is waiting for.
        answered = any(
            message.get("id") == self._call_id and ("result" in message or "error" in message)
            for message in self.sent
        )
        if answered:
            self._to_server.put_nowait(None)
        else:
            asyncio.get_running_loop().call_soon(self._close_when_done)


def _call(tool: str, **arguments) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


# -- sampling ---------------------------------------------------------


def test_sample_issues_create_message_and_returns_result():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Summarise via the client's model")
    async def summarise(text: str, ctx: MCPContext) -> str:
        result = await ctx.sample(
            [{"role": "user", "content": {"type": "text", "text": text}}],
            max_tokens=64,
            model_preferences={"intelligencePriority": 0.9},
            system_prompt="Be terse.",
        )
        return result["content"]["text"]

    def respond(request: dict) -> dict:
        assert request["method"] == "sampling/createMessage"
        params = request["params"]
        assert params["maxTokens"] == 64
        assert params["modelPreferences"] == {"intelligencePriority": 0.9}
        assert params["systemPrompt"] == "Be terse."
        return {
            "result": {
                "role": "assistant",
                "model": "test-model",
                "content": {"type": "text", "text": "short"},
            }
        }

    client = _Client(MCPServer(app), {"sampling": {}}, respond)
    sent = asyncio.run(client.run(_call("summarise", text="a long body")))

    assert len(client.requests) == 1
    # The tool's result carries the sampled text the client returned.
    call_response = next(m for m in sent if m.get("id") == 2)
    assert call_response["result"]["content"][0]["text"] == "short"


def test_sample_with_tools_requires_sampling_tools_capability():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Sample with tools")
    async def agentic(ctx: MCPContext) -> str:
        await ctx.sample(
            [{"role": "user", "content": {"type": "text", "text": "hi"}}],
            max_tokens=8,
            tools=[{"name": "search"}],
        )
        return "ok"

    client = _Client(MCPServer(app), {"sampling": {}}, lambda r: {"result": {}})
    sent = asyncio.run(client.run(_call("agentic")))

    # `sampling` without `sampling.tools` rejects a tool-enabled sample in-band.
    call_response = next(m for m in sent if m.get("id") == 2)
    assert call_response["result"]["isError"] is True
    assert client.requests == []


def test_sample_with_tools_allowed_when_sub_capability_advertised():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Sample with tools")
    async def agentic(ctx: MCPContext) -> str:
        await ctx.sample(
            [{"role": "user", "content": {"type": "text", "text": "hi"}}],
            max_tokens=8,
            tools=[{"name": "search"}],
            tool_choice={"type": "auto"},
        )
        return "ok"

    def respond(request: dict) -> dict:
        assert request["params"]["tools"] == [{"name": "search"}]
        assert request["params"]["toolChoice"] == {"type": "auto"}
        return {"result": {"role": "assistant", "content": {"type": "text", "text": "x"}}}

    client = _Client(MCPServer(app), {"sampling": {"tools": {}}}, respond)
    asyncio.run(client.run(_call("agentic")))
    assert len(client.requests) == 1


def test_sample_rejected_without_sampling_capability():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Sample")
    async def s(ctx: MCPContext) -> str:
        await ctx.sample([{"role": "user", "content": {"type": "text", "text": "x"}}], max_tokens=8)
        return "ok"

    client = _Client(MCPServer(app), {}, lambda r: {"result": {}})
    sent = asyncio.run(client.run(_call("s")))
    call_response = next(m for m in sent if m.get("id") == 2)
    assert call_response["result"]["isError"] is True
    assert client.requests == []


# -- elicitation ------------------------------------------------------


def test_elicit_form_mode_sends_requested_schema():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Ask for confirmation")
    async def confirm(ctx: MCPContext) -> str:
        result = await ctx.elicit(
            "Proceed?",
            requested_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        )
        return result["action"]

    def respond(request: dict) -> dict:
        assert request["method"] == "elicitation/create"
        assert request["params"]["message"] == "Proceed?"
        assert "requestedSchema" in request["params"]
        assert "mode" not in request["params"]
        return {"result": {"action": "accept", "content": {"ok": True}}}

    client = _Client(MCPServer(app), {"elicitation": {}}, respond)
    sent = asyncio.run(client.run(_call("confirm")))
    call_response = next(m for m in sent if m.get("id") == 2)
    assert call_response["result"]["content"][0]["text"] == "accept"


def test_elicit_url_mode_sends_url():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Open a URL")
    async def authorize(ctx: MCPContext) -> str:
        result = await ctx.elicit("Sign in", url="https://example.test/auth")
        return result["action"]

    def respond(request: dict) -> dict:
        assert request["params"]["mode"] == "url"
        assert request["params"]["url"] == "https://example.test/auth"
        # Required by the spec so a later completion notification can name it.
        assert request["params"]["elicitationId"]
        return {"result": {"action": "accept"}}

    # The client must declare `elicitation.url`; the bare `{"elicitation": {}}`
    # this once used means form-only, and URL mode is refused for it.
    client = _Client(MCPServer(app), {"elicitation": {"url": {}}}, respond)
    asyncio.run(client.run(_call("authorize")))
    assert len(client.requests) == 1


def test_elicit_rejected_without_capability():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Elicit")
    async def e(ctx: MCPContext) -> str:
        await ctx.elicit("hi")
        return "ok"

    client = _Client(MCPServer(app), {}, lambda r: {"result": {}})
    sent = asyncio.run(client.run(_call("e")))
    call_response = next(m for m in sent if m.get("id") == 2)
    assert call_response["result"]["isError"] is True
    assert client.requests == []


# -- roots ------------------------------------------------------------


def test_roots_lists_client_roots():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Count roots")
    async def count_roots(ctx: MCPContext) -> int:
        roots = await ctx.roots()
        return len(roots)

    def respond(request: dict) -> dict:
        assert request["method"] == "roots/list"
        return {"result": {"roots": [{"uri": "file:///a"}, {"uri": "file:///b"}]}}

    client = _Client(MCPServer(app), {"roots": {"listChanged": True}}, respond)
    sent = asyncio.run(client.run(_call("count_roots")))
    call_response = next(m for m in sent if m.get("id") == 2)
    assert call_response["result"]["content"][0]["text"] == "2"


def test_roots_rejected_without_capability():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Roots")
    async def r(ctx: MCPContext) -> int:
        return len(await ctx.roots())

    client = _Client(MCPServer(app), {}, lambda req: {"result": {}})
    sent = asyncio.run(client.run(_call("r")))
    call_response = next(m for m in sent if m.get("id") == 2)
    assert call_response["result"]["isError"] is True
    assert client.requests == []


# -- error plumbing ---------------------------------------------------


def test_client_error_reply_surfaces_as_request_error():
    app = Veloce(openapi_url=None)
    captured: dict = {}

    @app.mcp_tool(description="Sample, catching a client refusal")
    async def s(ctx: MCPContext) -> str:
        try:
            await ctx.sample(
                [{"role": "user", "content": {"type": "text", "text": "x"}}], max_tokens=8
            )
        except MCPRequestError as exc:
            captured["error"] = str(exc)
        return "done"

    def respond(request: dict) -> dict:
        return {"error": {"code": -1, "message": "user declined"}}

    client = _Client(MCPServer(app), {"sampling": {}}, respond)
    asyncio.run(client.run(_call("s")))
    assert "user declined" in captured["error"]


# -- off-transport / bare context ------------------------------------


def test_bare_context_request_methods_raise_runtime_error():
    ctx = MCPContext("t", client_capabilities={"sampling": {}})

    async def go() -> None:
        with pytest.raises(RuntimeError):
            await ctx.sample([], max_tokens=4)

    asyncio.run(go())


def test_capability_gate_raises_capability_error_directly():
    async def requester(method: str, params: dict) -> dict:
        return {}

    ctx = MCPContext("t", requester=requester, client_capabilities={})

    async def go() -> None:
        with pytest.raises(MCPCapabilityError):
            await ctx.roots()

    asyncio.run(go())


def test_elicit_rejects_both_form_and_url():
    async def requester(method: str, params: dict) -> dict:
        return {}

    ctx = MCPContext("t", requester=requester, client_capabilities={"elicitation": {}})

    async def go() -> None:
        with pytest.raises(ValueError):
            await ctx.elicit("hi", requested_schema={"type": "object"}, url="https://x.test")

    asyncio.run(go())
