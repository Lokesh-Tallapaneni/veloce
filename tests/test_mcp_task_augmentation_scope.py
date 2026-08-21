"""Which methods accept a task-augmented request.

Only `tools/call` runs a primitive in the background. `resources/read` and
`prompts/get` used to accept the `task` field and answer synchronously anyway,
so a caller asking for background execution received an ordinary result and no
signal - and could then wait on a handle that was never issued. Both now refuse
the request instead of reinterpreting it.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.tasks import TASKS_EXTENSION

MODERN = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}},
}


def _app() -> Veloce:
    app = Veloce(title="TaskScope", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="A task-capable tool", task_support=True)
    async def slow(value: int = 1) -> dict:
        return {"value": value}

    @app.mcp_tool(description="An ordinary tool")
    async def quick() -> dict:
        return {"ok": True}

    @app.mcp_prompt(description="Greet someone")
    async def greet(name: str) -> str:
        return f"Hello {name}"

    @app.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_resource_uri="res://doc",
        mcp_description="A document",
    )
    async def doc() -> dict:
        return {"body": "text"}

    return app


async def _send(method: str, params: dict, *, modern: bool = False) -> dict:
    payload = dict(params)
    if modern:
        payload["_meta"] = MODERN
    return await MCPServer(_app()).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": payload},
        MCPSession(),
    )


# ── The two synchronous methods refuse ───────────────────────────────


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("resources/read", {"uri": "res://doc"}),
        ("prompts/get", {"name": "greet", "arguments": {"name": "ada"}}),
    ],
)
async def test_a_task_augmented_request_is_refused(method: str, params: dict):
    response = await _send(method, {**params, "task": {}})
    assert "error" in response, response
    assert response["error"]["code"] == -32602


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("resources/read", {"uri": "res://doc"}),
        ("prompts/get", {"name": "greet", "arguments": {"name": "ada"}}),
    ],
)
async def test_the_refusal_names_the_method_and_the_way_forward(method: str, params: dict):
    response = await _send(method, {**params, "task": {}})
    message = response["error"]["message"]
    assert method in message
    assert "without a 'task' field" in message


async def test_the_refusal_holds_on_the_modern_revision_too():
    response = await _send("resources/read", {"uri": "res://doc", "task": {}}, modern=True)
    assert response["error"]["code"] == -32602


async def test_a_task_field_carrying_options_is_still_refused():
    """The field's contents do not matter; the method cannot honour any of them."""
    response = await _send("resources/read", {"uri": "res://doc", "task": {"ttl": 60000}})
    assert response["error"]["code"] == -32602


# ── Requests without the field are unaffected ────────────────────────


async def test_an_ordinary_resource_read_still_succeeds():
    response = await _send("resources/read", {"uri": "res://doc"})
    assert "error" not in response
    assert response["result"]["contents"][0]["uri"] == "res://doc"


async def test_an_ordinary_prompt_render_still_succeeds():
    response = await _send("prompts/get", {"name": "greet", "arguments": {"name": "ada"}})
    assert "error" not in response
    assert "ada" in response["result"]["messages"][0]["content"]["text"]


# ── tools/call still honours the field ───────────────────────────────


async def test_a_task_capable_tool_still_returns_a_handle():
    response = await _send("tools/call", {"name": "slow", "arguments": {}, "task": {}}, modern=True)
    assert response["result"]["resultType"] == "task"
    assert response["result"]["task"]["taskId"]


async def test_a_tool_that_did_not_opt_in_reports_that_it_cannot():
    response = await _send(
        "tools/call", {"name": "quick", "arguments": {}, "task": {}}, modern=True
    )
    assert response["error"]["code"] == -32602
    assert "does not support task execution" in response["error"]["message"]


async def test_an_ordinary_tool_call_is_unaffected():
    response = await _send("tools/call", {"name": "quick", "arguments": {}})
    assert "error" not in response
    assert not response["result"].get("isError")
