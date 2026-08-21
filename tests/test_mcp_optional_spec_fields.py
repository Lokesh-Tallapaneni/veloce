"""Optional spec fields an author had no way to set.

Each of these is a real, typed, optional field in the protocol that Veloce never
emitted because nothing could supply a value: the identity a server publishes
about itself, and the context a sampling request may ask for.

They are optional, so omitting one is legal — but being unable to set it means an
application cannot say something the protocol has a place for.
"""

from __future__ import annotations

import pytest

from veloce import MCPContext, Veloce
from veloce.contrib.mcp import Icon
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession

ICON = Icon(src="https://example.com/icon.svg", mime_type="image/svg+xml", sizes=["any"])


async def _server_info(app: Veloce) -> dict:
    response = await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, MCPSession()
    )
    return response["result"]["serverInfo"]


def _app(**kwargs) -> Veloce:
    kwargs.setdefault("title", "Ledger")
    kwargs.setdefault("version", "2.1.0")
    app = Veloce(openapi_url=None, **kwargs)

    @app.mcp_tool(description="A tool")
    async def tool() -> int:
        return 1

    return app


# ── Server identity ──────────────────────────────────────────────────


async def test_a_website_url_is_published():
    info = await _server_info(_app(website_url="https://example.com/ledger"))
    assert info["websiteUrl"] == "https://example.com/ledger"


async def test_icons_are_published():
    info = await _server_info(_app(mcp_icons=[ICON]))
    assert info["icons"] == [
        {"src": "https://example.com/icon.svg", "mimeType": "image/svg+xml", "sizes": ["any"]}
    ]


async def test_the_name_and_version_are_unchanged():
    info = await _server_info(_app(website_url="https://example.com/ledger"))
    assert info["name"] == "Ledger"
    assert info["version"] == "2.1.0"


async def test_neither_field_is_emitted_when_unset():
    """An app that says nothing publishes nothing, rather than an empty value."""
    info = await _server_info(_app())
    assert "websiteUrl" not in info
    assert "icons" not in info


async def test_the_same_identity_reaches_the_modern_discovery_probe():
    """`server/discover` carries `serverInfo` in `_meta`; it must agree."""
    app = _app(website_url="https://example.com/ledger", mcp_icons=[ICON])
    response = await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}, MCPSession()
    )
    info = next(iter(response["result"]["_meta"].values()))
    assert info["websiteUrl"] == "https://example.com/ledger"
    assert info["icons"]


# ── Sampling context ─────────────────────────────────────────────────


def _sampling_context(sent: list) -> MCPContext:
    async def requester(method: str, params: dict) -> dict:
        sent.append((method, params))
        return {"role": "assistant", "content": {"type": "text", "text": "ok"}}

    context = MCPContext("probe")
    context._requester = requester
    context._client_capabilities = {"sampling": {}}
    return context


@pytest.mark.parametrize("mode", ["none", "thisServer", "allServers"])
async def test_a_sampling_request_may_ask_for_context(mode: str):
    sent: list = []
    await _sampling_context(sent).sample(
        [{"role": "user", "content": {"type": "text", "text": "hi"}}],
        max_tokens=16,
        include_context=mode,
    )
    assert sent[0][1]["includeContext"] == mode


async def test_asking_for_nothing_sends_no_field():
    sent: list = []
    await _sampling_context(sent).sample(
        [{"role": "user", "content": {"type": "text", "text": "hi"}}], max_tokens=16
    )
    assert "includeContext" not in sent[0][1]


async def test_a_mode_the_spec_does_not_define_is_refused():
    """A client would silently drop it, so the typo surfaces here instead."""
    sent: list = []
    with pytest.raises(ValueError, match="include_context must be one of"):
        await _sampling_context(sent).sample(
            [{"role": "user", "content": {"type": "text", "text": "hi"}}],
            max_tokens=16,
            include_context="thisserver",
        )
    assert sent == []


async def test_the_other_sampling_fields_are_unaffected():
    sent: list = []
    await _sampling_context(sent).sample(
        [{"role": "user", "content": {"type": "text", "text": "hi"}}],
        max_tokens=32,
        system_prompt="be brief",
        temperature=0.2,
        include_context="thisServer",
    )
    _method, params = sent[0]
    assert params["maxTokens"] == 32
    assert params["systemPrompt"] == "be brief"
    assert params["temperature"] == 0.2
    assert params["includeContext"] == "thisServer"
