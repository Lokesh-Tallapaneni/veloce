"""Exposing prompts, and what `prompts/get` returns.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import pytest

from tests._mcp_shared import (
    _get_prompt,
    _initialize,
    _list_prompts,
    _server,
)
from veloce import (
    Depends,
    MCPContext,
    Veloce,
)
from veloce.app.mcp import MCPPromptRegistration

# -- Prompts ----------------------------------------------------------


def test_prompt_is_listed_with_arguments():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Summarise a topic")
    async def summarise(topic: str, style: str = "concise") -> str:
        return f"Summarise {topic} ({style})."

    listed = _list_prompts(app)
    assert "summarise" in listed
    entry = listed["summarise"]
    assert entry["description"] == "Summarise a topic"
    args = {a["name"]: a for a in entry["arguments"]}
    assert args["topic"]["required"] is True
    # A parameter with a default is an optional argument.
    assert args["style"]["required"] is False


def test_prompt_get_string_return_is_user_message():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Greeting")
    async def greet() -> str:
        return "Hello there."

    out = _get_prompt(app, "greet")
    assert "error" not in out
    result = out["result"]
    assert result["description"] == "Greeting"
    assert result["messages"] == [
        {"role": "user", "content": {"type": "text", "text": "Hello there."}}
    ]


def test_prompt_get_passes_arguments():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Summarise a topic")
    async def summarise(topic: str) -> str:
        return f"Summarise {topic} in three bullet points."

    result = _get_prompt(app, "summarise", {"topic": "veloce"})["result"]
    text = result["messages"][0]["content"]["text"]
    assert text == "Summarise veloce in three bullet points."


def test_prompt_get_message_list_is_normalised():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="A two-turn exchange")
    async def chat() -> list:
        return [
            {"role": "assistant", "content": "How can I help?"},
            {"role": "user", "content": {"type": "text", "text": "Explain MCP."}},
        ]

    messages = _get_prompt(app, "chat")["result"]["messages"]
    assert messages[0] == {
        "role": "assistant",
        "content": {"type": "text", "text": "How can I help?"},
    }
    assert messages[1] == {
        "role": "user",
        "content": {"type": "text", "text": "Explain MCP."},
    }


def test_prompt_unknown_name_is_invalid_params():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Greeting")
    async def greet() -> str:
        return "hi"

    out = _get_prompt(app, "nope")
    assert out["error"]["code"] == -32602


def test_prompt_missing_required_argument_is_invalid_params():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Summarise a topic")
    async def summarise(topic: str) -> str:
        return f"Summarise {topic}."

    out = _get_prompt(app, "summarise", {})
    assert out["error"]["code"] == -32602


def test_prompt_dependency_is_resolved():
    app = Veloce(openapi_url=None)

    def tone() -> str:
        return "friendly"

    @app.mcp_prompt(description="Greeting in a tone")
    async def greet(style: str = Depends(tone)) -> str:
        return f"Say hello in a {style} tone."

    # `style` is injected, so it is not advertised as a prompt argument.
    assert _list_prompts(app)["greet"].get("arguments", []) == []
    text = _get_prompt(app, "greet")["result"]["messages"][0]["content"]["text"]
    assert text == "Say hello in a friendly tone."


def test_prompt_context_is_injected():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Echo the prompt name")
    async def whoami(ctx: MCPContext) -> str:
        return ctx.tool_name

    # The context parameter is not advertised as an argument.
    assert _list_prompts(app)["whoami"].get("arguments", []) == []
    text = _get_prompt(app, "whoami")["result"]["messages"][0]["content"]["text"]
    assert text == "whoami"


def test_prompt_sync_handler_is_offloaded():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Sync greeting")
    def greet(name: str) -> str:
        return f"Hello, {name}."

    text = _get_prompt(app, "greet", {"name": "ada"})["result"]["messages"][0]["content"]["text"]
    assert text == "Hello, ada."


def test_prompt_namespace_prefixes_name():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Namespaced", namespace="docs")
    async def intro() -> str:
        return "Intro."

    assert "docs_intro" in _list_prompts(app)


def test_prompt_duplicate_name_raises():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="One")
    async def greet() -> str:
        return "one"

    # A second registration under the same name, built the way the decorator
    # builds one. It used to be a seven-element positional tuple.
    app._mcp_prompts.append(
        MCPPromptRegistration(
            handler=greet,
            name="greet",
            description="Two",
            namespace=None,
            scopes=None,
            icons=None,
            meta=None,
        )
    )
    with pytest.raises(ValueError, match="Duplicate MCP prompt"):
        _server(app)


def test_prompt_missing_description_raises():
    app = Veloce(openapi_url=None)

    with pytest.raises(ValueError, match="description"):

        @app.mcp_prompt(description="")
        async def bad() -> str:
            return "x"


def test_initialize_advertises_prompts_when_present():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Greeting")
    async def greet() -> str:
        return "hi"

    caps = _initialize(app, {})["result"]["capabilities"]
    # True over the stdio pipe: a stateful connection can be told its prompt
    # listing changed, which `MCPContext.hide` can do.
    assert caps["prompts"] == {"listChanged": True}


def test_initialize_omits_prompts_capability_when_none():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    caps = _initialize(app, {})["result"]["capabilities"]
    assert "prompts" not in caps
