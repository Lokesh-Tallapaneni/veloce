"""`resultType` is applied once per modern result, and never to a legacy one.

The tagging ran as a three-way ladder whose first arm was an uncommented bare
`pass` and whose shared two-term guard (`is_modern and isinstance(result, dict)`)
was repeated on all three arms. The guard is hoisted and the "already tagged"
case says so in a comment instead of being a bare `pass`.

That is a restructuring of the branch deciding what every modern MCP client
reads, so each arm is covered here along with the legacy path - the one a
mis-hoisted guard would break first.

Driven through `handle_message`, the public entry point. A modern client never
sends `initialize`: it states its protocol version in `params._meta` on **every**
request, and that is what selects the era - so these build the messages a modern
client actually sends rather than performing a handshake, which is what an
earlier draft of this module got wrong.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.mcp.server import MODERN_PROTOCOL_VERSION, MCPServer
from veloce.contrib.mcp.session import MCPSession

# The revision at which the modern envelope begins, read from the source of
# truth rather than pinned as a literal - an earlier draft of this module used
# `2025-06-18`, which `is_modern_version` answers False for, and every "modern"
# assertion silently exercised the legacy path instead.
MODERN = MODERN_PROTOCOL_VERSION
LEGACY = "2024-11-05"


META_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"


def _server() -> MCPServer:
    app = Veloce(title="T", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Adds two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    return MCPServer(app)


def _message(method: str, params: dict, *, modern: bool, msg_id: int = 2) -> dict:
    """One JSON-RPC request, in the era a client of that revision would send.

    A modern client tags every request with its protocol version in `_meta`; a
    legacy one sends none, and the absence is what selects the legacy era.
    """
    payload = dict(params)
    if modern:
        payload["_meta"] = {META_VERSION_KEY: MODERN}
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": payload}


async def _result(modern: bool, method: str, params: dict) -> dict:
    server, session = _server(), MCPSession()
    response = await server.handle_message(_message(method, params, modern=modern), session)
    return response["result"]


LISTING = ("tools/list", {})
CALL = ("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}})


# ── a modern result is tagged ────────────────────────────────────────


@pytest.mark.parametrize("case", [LISTING, CALL], ids=["tools/list", "tools/call"])
async def test_a_modern_result_carries_a_result_type(case):
    assert "resultType" in await _result(True, *case)


async def test_a_modern_listing_is_tagged_complete():
    assert (await _result(True, *LISTING))["resultType"] == "complete"


async def test_the_tag_does_not_replace_the_answer():
    """The arms rebuild the dict; its own keys must survive."""
    result = await _result(True, *LISTING)
    assert result["tools"][0]["name"] == "add"


async def test_a_modern_call_keeps_its_content():
    assert "content" in await _result(True, *CALL)


# ── and a legacy result is not ───────────────────────────────────────
#
# The negative that matters: a mis-hoisted guard would tag these too, and a
# legacy client's revision has no such field.


@pytest.mark.parametrize("case", [LISTING, CALL], ids=["tools/list", "tools/call"])
async def test_a_legacy_result_carries_no_result_type(case):
    assert "resultType" not in await _result(False, *case)


async def test_a_legacy_listing_still_returns_its_tools():
    assert (await _result(False, *LISTING))["tools"][0]["name"] == "add"


async def test_a_legacy_call_still_returns_its_content():
    """The tag is the only thing that differs; the answer must not."""
    assert "content" in await _result(False, *CALL)


async def test_modern_and_legacy_agree_on_the_answer_itself():
    """The answer is the same; the modern envelope only adds to it.

    A cacheable method also gains `cacheScope` / `ttlMs` from the same branch,
    so the two eras are not key-for-key equal - what must hold is that every
    field the legacy client sees is present and identical for the modern one.
    """
    modern = await _result(True, *LISTING)
    legacy = await _result(False, *LISTING)
    assert set(legacy) <= set(modern)
    for key, value in legacy.items():
        assert modern[key] == value, key


async def test_a_non_cacheable_method_differs_only_by_the_tag():
    """`tools/call` gets no cache hints, so the tag is the whole difference."""
    modern = await _result(True, *CALL)
    legacy = await _result(False, *CALL)
    assert modern.pop("resultType") == "complete"
    assert modern == legacy


async def test_only_a_modern_result_carries_cache_hints():
    """The other half of the same branch, pinned so hoisting the guard cannot
    start leaking modern-only fields to a legacy client."""
    modern = await _result(True, *LISTING)
    legacy = await _result(False, *LISTING)
    assert "cacheScope" in modern
    assert "cacheScope" not in legacy


# ── the tag is applied once ──────────────────────────────────────────


async def test_a_repeated_request_is_tagged_once_each_time():
    """The arm that used to be a bare `pass`: the tag is applied exactly once,
    and a second identical request gets the same one rather than a doubled tag."""
    server, session = _server(), MCPSession()
    for msg_id in (2, 3):
        message = _message(*LISTING, modern=True, msg_id=msg_id)
        result = (await server.handle_message(message, session))["result"]
        assert result["resultType"] == "complete"
        assert list(result).count("resultType") == 1


async def test_a_notification_produces_no_response_at_all():
    """The `return None` above the ladder; a hoisted guard must not swallow it."""
    server, session = _server(), MCPSession()
    message = {"jsonrpc": "2.0", "method": "ping", "params": {"_meta": {META_VERSION_KEY: MODERN}}}
    assert await server.handle_message(message, session) is None


async def test_a_ping_still_answers_under_the_modern_era():
    """The `isinstance(result, dict)` half of the hoisted guard."""
    server, session = _server(), MCPSession()
    response = await server.handle_message(_message("ping", {}, modern=True, msg_id=9), session)
    assert response["id"] == 9


# ── the already-tagged arm ───────────────────────────────────────────
#
# Nothing in-tree returns a result that already carries `resultType`, so the
# first arm is defensive - and unreachable without arranging it. Mutation-testing
# proved that: making the arm re-tag instead of leaving the result alone broke
# nothing until this test existed. The arm is a real contract (a handler that
# tags its own result keeps that tag), so it is reached here by substituting a
# handler that returns one.


async def test_a_pre_tagged_result_keeps_its_own_tag(monkeypatch):
    """The arm that used to be a bare `pass`, made reachable."""
    server, session = _server(), MCPSession()

    async def pre_tagged(_params):
        return {"resultType": "custom", "value": 1}

    monkeypatch.setitem(server._methods, "tools/list", pre_tagged)

    result = (await server.handle_message(_message(*LISTING, modern=True), session))["result"]
    assert result["resultType"] == "custom"
    assert result["value"] == 1


async def test_a_pre_tagged_result_is_not_given_cache_hints(monkeypatch):
    """The other consequence of taking the first arm: the complete-arm's cache
    hints belong to results this server tagged, not to one that arrived tagged."""
    server, session = _server(), MCPSession()

    async def pre_tagged(_params):
        return {"resultType": "custom", "value": 1}

    monkeypatch.setitem(server._methods, "tools/list", pre_tagged)
    result = (await server.handle_message(_message(*LISTING, modern=True), session))["result"]
    assert "cacheScope" not in result
