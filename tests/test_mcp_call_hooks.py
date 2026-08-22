"""Hooks that run around every MCP call, whichever way the tool was registered.

A route-backed tool replays the HTTP request lifecycle, so `before_request` and
friends already see it. A tool registered with `@app.mcp_tool` has no route, so
nothing ran around it at all — there was no supported place to put auth, an audit
log, or a rate limit that covers every tool a server exposes.

`Depends` and declared `mcp_scopes` still work per tool; these are what was
missing at the server level.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.mcp.errors import AuthorizationError
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


def _app() -> Veloce:
    app = Veloce(title="Hooked", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="A tool with no route")
    async def pure(a: int = 1) -> int:
        return a * 2

    @app.get("/routed", expose_as_mcp_tool=True, mcp_description="A tool from a route")
    async def routed() -> dict:
        return {"ok": True}

    @app.mcp_prompt(description="A prompt")
    async def prompt() -> str:
        return "hello"

    @app.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_resource_uri="doc://one",
        mcp_description="A resource",
    )
    async def doc() -> dict:
        return {"text": "hi"}

    return app


async def _call(app: Veloce, name: str, arguments: dict | None = None) -> dict:
    response = await MCPServer(app).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        MCPSession(),
    )
    return response["result"]


# ── Both registration styles are covered ─────────────────────────────


@pytest.mark.parametrize("tool", ["pure", "routed"])
async def test_a_hook_runs_around_either_kind_of_tool(tool: str):
    app = _app()
    seen: list = []

    @app.before_mcp_call
    async def before(name, arguments):
        seen.append(("before", name))

    @app.after_mcp_call
    async def after(name, result):
        seen.append(("after", name))
        return result

    await _call(app, tool)
    assert seen == [("before", tool), ("after", tool)]


async def test_the_hook_sees_the_arguments_the_call_carried():
    app = _app()
    seen: list = []

    @app.before_mcp_call
    async def before(name, arguments):
        seen.append(dict(arguments))

    await _call(app, "pure", {"a": 21})
    assert seen == [{"a": 21}]


async def test_a_sync_hook_is_supported():
    app = _app()
    seen: list = []

    @app.before_mcp_call
    def before(name, arguments):
        seen.append(name)

    await _call(app, "pure")
    assert seen == ["pure"]


async def test_a_resource_read_runs_the_hooks_too():
    app = _app()
    seen: list = []

    @app.before_mcp_call
    async def before(name, arguments):
        seen.append(name)

    await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "doc://one"}},
        MCPSession(),
    )
    assert seen == ["doc"]


async def test_a_prompt_render_runs_the_hooks_too():
    app = _app()
    seen: list = []

    @app.before_mcp_call
    async def before(name, arguments):
        seen.append(name)

    await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "prompts/get", "params": {"name": "prompt"}},
        MCPSession(),
    )
    assert seen == ["prompt"]


# ── A hook can answer instead of the handler ─────────────────────────


async def test_returning_a_value_answers_without_calling_the_handler():
    app = _app()
    called: list = []

    @app.mcp_tool(description="Records that it ran")
    async def records() -> str:
        called.append(True)
        return "handler"

    @app.before_mcp_call
    async def cached(name, arguments):
        if name == "records":
            return "from the hook"
        return None

    assert (await _call(app, "records"))["content"][0]["text"] == "from the hook"
    assert called == [], "the handler must not have run"


async def test_returning_none_lets_the_call_proceed():
    app = _app()

    @app.before_mcp_call
    async def passive(name, arguments):
        return None

    assert (await _call(app, "pure", {"a": 5}))["content"][0]["text"] == "10"


async def test_a_hook_refuses_a_call_by_raising_an_authorization_error():
    """How an authorization check says no: the same error a scoped tool raises."""
    app = _app()

    @app.before_mcp_call
    async def deny(name, arguments):
        raise AuthorizationError("mcp:admin")

    response = await MCPServer(app).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "pure", "arguments": {}},
        },
        MCPSession(),
    )
    assert "error" in response, "an authorization refusal is a protocol error"


async def test_any_other_raising_hook_is_reported_like_a_failing_handler():
    """A hook is code; a bug in one is reported in band, not as a crash."""
    app = _app()

    @app.before_mcp_call
    async def broken(name, arguments):
        raise ValueError("hook bug")

    result = await _call(app, "pure")
    assert result["isError"] is True


# ── An after hook can rewrite the result ─────────────────────────────


async def test_an_after_hook_may_replace_the_result():
    app = _app()

    @app.after_mcp_call
    async def redact(name, result):
        return "redacted"

    assert (await _call(app, "pure"))["content"][0]["text"] == "redacted"


async def test_after_hooks_chain_in_registration_order():
    app = _app()

    @app.after_mcp_call
    async def first(name, result):
        return f"{result}-one"

    @app.after_mcp_call
    async def second(name, result):
        return f"{result}-two"

    assert (await _call(app, "pure", {"a": 1}))["content"][0]["text"] == "2-one-two"


async def test_an_after_hook_does_not_run_when_the_call_raised():
    app = _app()
    seen: list = []

    @app.mcp_tool(description="Fails")
    async def boom() -> int:
        raise ValueError("no")

    @app.after_mcp_call
    async def after(name, result):
        seen.append(name)
        return result

    assert (await _call(app, "boom"))["isError"] is True
    assert seen == []


# ── Registering none costs nothing ───────────────────────────────────


async def test_an_app_with_no_hooks_behaves_exactly_as_before():
    app = _app()
    assert app._mcp_before_call == []
    assert app._mcp_after_call == []
    assert (await _call(app, "pure", {"a": 4}))["content"][0]["text"] == "8"


def test_the_decorators_return_the_function_unchanged():
    app = _app()

    async def hook(name, arguments):
        return None

    assert app.before_mcp_call(hook) is hook
    assert app.after_mcp_call(hook) is hook
