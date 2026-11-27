"""MCP argument completion - capability advertisement, completion/complete, registration."""

from __future__ import annotations

import pytest

from tests._mcp import INVALID_PARAMS, call, greeting_server
from veloce import Veloce
from veloce.contrib.mcp import CompletionResult, CompletionsCapability
from veloce.contrib.mcp.completion import _MAX_CONTEXT_ARGS
from veloce.contrib.mcp.server import MCPServer
from veloce.principal import Principal, set_principal


def _server(app: Veloce) -> MCPServer:
    return MCPServer(app)


async def _complete(server: MCPServer, ref: dict, name: str, value: str, **extra) -> dict:
    params: dict = {"ref": ref, "argument": {"name": name, "value": value}, **extra}
    out = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "completion/complete", "params": params}
    )
    return out["result"]


# -- Advertisement ----------------------------------------------------


async def test_completions_not_advertised_without_a_completer():
    """A server with no registered completer advertises no completions capability."""
    server = greeting_server()
    assert "completions" not in (await call(server, "initialize", {}))["capabilities"]
    assert "completion/complete" in server._methods


async def test_completions_advertised_when_a_completer_exists():
    """Registering a completer surfaces the completions capability."""

    async def complete_name(value, context):
        return ["ada", "alan"]

    server = greeting_server(complete_name)
    assert (await call(server, "initialize", {}))["capabilities"]["completions"] == {}


# -- completion/complete on a prompt ----------------------------------


async def test_prompt_completion_returns_candidate_values():
    """A prompt-argument completer answers completion/complete with its candidates."""

    async def complete_name(value, context):
        return [n for n in ("ada", "alan", "grace") if n.startswith(value)]

    server = greeting_server(complete_name)
    result = await _complete(server, {"type": "ref/prompt", "name": "greet"}, "name", "a")
    assert result == {"completion": {"values": ["ada", "alan"], "total": 2, "hasMore": False}}


async def test_sync_completer_is_supported():
    """A sync completer is offloaded and its values returned."""

    def complete_name(value, context):
        return ["bob"]

    server = greeting_server(complete_name)
    result = await _complete(server, {"type": "ref/prompt", "name": "greet"}, "name", "")
    assert result["completion"]["values"] == ["bob"]


async def test_completer_receives_sibling_context_arguments():
    """A completer sees the already-resolved sibling argument values."""
    app = Veloce()

    @app.mcp_prompt(description="Pick a city in a country")
    async def trip(country: str, city: str) -> str:
        return f"{city}, {country}"

    seen: dict = {}

    @app.mcp_completer(prompt="trip", argument="city")
    async def complete_city(value, context):
        seen.update(context)
        return ["paris"] if context.get("country") == "fr" else []

    server = _server(app)
    result = await _complete(
        server,
        {"type": "ref/prompt", "name": "trip"},
        "city",
        "p",
        context={"arguments": {"country": "fr"}},
    )
    assert seen == {"country": "fr"}
    assert result["completion"]["values"] == ["paris"]


async def test_completion_result_carries_explicit_totals():
    """A CompletionResult declares total / hasMore explicitly."""

    async def complete_name(value, context):
        return CompletionResult(["x", "y"], total=99, has_more=True)

    server = greeting_server(complete_name)
    result = await _complete(server, {"type": "ref/prompt", "name": "greet"}, "name", "")
    assert result == {"completion": {"values": ["x", "y"], "total": 99, "hasMore": True}}


async def test_over_cap_values_are_truncated_and_flagged():
    """More than 100 candidate values are capped, with total and hasMore reflecting overflow."""

    async def complete_name(value, context):
        return [str(i) for i in range(150)]

    server = greeting_server(complete_name)
    result = await _complete(server, {"type": "ref/prompt", "name": "greet"}, "name", "")
    completion = result["completion"]
    assert len(completion["values"]) == 100
    assert completion["total"] == 150
    assert completion["hasMore"] is True


# -- Empty completion (no completer) ----------------------------------


async def test_argument_without_a_completer_returns_empty():
    """An argument with no registered completer answers with an empty completion."""
    app = Veloce()

    @app.mcp_prompt(description="A greeting")
    async def greet(name: str, title: str) -> str:
        return f"{title} {name}"

    @app.mcp_completer(prompt="greet", argument="name")
    async def complete_name(value, context):
        return ["ada"]

    server = _server(app)
    result = await _complete(server, {"type": "ref/prompt", "name": "greet"}, "title", "")
    assert result == {"completion": {"values": [], "hasMore": False}}


async def test_unknown_prompt_reference_returns_empty():
    """A reference to an unregistered prompt answers empty, not an error."""

    async def complete_name(value, context):
        return ["ada"]

    server = greeting_server(complete_name)
    result = await _complete(server, {"type": "ref/prompt", "name": "missing"}, "name", "")
    assert result == {"completion": {"values": [], "hasMore": False}}


# -- completion/complete on a resource template -----------------------


async def test_resource_template_completion():
    """A resource-template-argument completer answers via a ref/resource reference."""
    app = Veloce()

    @app.get(
        "/users/{user_id}",
        expose_as_mcp_resource=True,
        mcp_description="A user",
        mcp_resource_uri="users://{user_id}",
    )
    async def get_user(user_id: str) -> dict:
        return {"id": user_id}

    @app.mcp_completer(resource="users://{user_id}", argument="user_id")
    async def complete_uid(value, context):
        return ["1", "2", "3"]

    server = _server(app)
    result = await _complete(
        server, {"type": "ref/resource", "uri": "users://{user_id}"}, "user_id", "1"
    )
    assert result["completion"]["values"] == ["1", "2", "3"]


# -- Malformed requests ----------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"argument": {"name": "name", "value": ""}},  # missing ref
        {"ref": {"type": "ref/prompt", "name": "greet"}},  # missing argument
        {"ref": {"type": "ref/bad"}, "argument": {"name": "n", "value": ""}},  # bad ref type
        {
            "ref": {"type": "ref/prompt", "name": "greet"},
            "argument": {"name": "name", "value": 5},
        },  # non-string value
    ],
)
async def test_malformed_request_is_invalid_params(params):
    """A malformed completion request is a JSON-RPC invalid-params error."""

    async def complete_name(value, context):
        return ["ada"]

    server = greeting_server(complete_name)
    out = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "completion/complete", "params": params}
    )
    assert out["error"]["code"] == INVALID_PARAMS


async def test_oversized_context_arguments_is_bounded():
    """An oversized client `context.arguments` is rejected, not materialized wholesale."""

    async def complete_name(value, context):
        return ["ada"]

    server = greeting_server(complete_name)
    oversized = {str(i): "x" for i in range(_MAX_CONTEXT_ARGS + 1)}
    out = await _complete_raw(
        server,
        {"type": "ref/prompt", "name": "greet"},
        "name",
        "",
        context={"arguments": oversized},
    )
    assert out["error"]["code"] == INVALID_PARAMS

    # A context at the cap is still accepted.
    at_cap = {str(i): "x" for i in range(_MAX_CONTEXT_ARGS)}
    ok = await _complete(
        server,
        {"type": "ref/prompt", "name": "greet"},
        "name",
        "",
        context={"arguments": at_cap},
    )
    assert ok["completion"]["values"] == ["ada"]


async def _complete_raw(server: MCPServer, ref: dict, name: str, value: str, **extra) -> dict:
    params: dict = {"ref": ref, "argument": {"name": name, "value": value}, **extra}
    return await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "completion/complete", "params": params}
    )


# -- Registration validation -----------------------------------------


def test_completer_for_unknown_prompt_raises():
    """A completer naming an unregistered prompt fails at build time."""
    app = Veloce()

    @app.mcp_completer(prompt="missing", argument="name")
    async def complete_name(value, context):
        return []

    with pytest.raises(ValueError, match="not a registered MCP prompt"):
        _server(app)


def test_completer_for_unknown_argument_raises():
    """A completer naming an argument the prompt lacks fails at build time."""
    app = Veloce()

    @app.mcp_prompt(description="A greeting")
    async def greet(name: str) -> str:
        return f"Hi {name}"

    @app.mcp_completer(prompt="greet", argument="nope")
    async def complete_nope(value, context):
        return []

    with pytest.raises(ValueError, match="no such argument"):
        _server(app)


def test_duplicate_completer_for_argument_raises():
    """Two completers for the same argument fail at build time."""
    app = Veloce()

    @app.mcp_prompt(description="A greeting")
    async def greet(name: str) -> str:
        return f"Hi {name}"

    @app.mcp_completer(prompt="greet", argument="name")
    async def first(value, context):
        return []

    @app.mcp_completer(prompt="greet", argument="name")
    async def second(value, context):
        return []

    with pytest.raises(ValueError, match="Duplicate MCP completer"):
        _server(app)


def test_completer_requires_exactly_one_target():
    """Passing both or neither of prompt= / resource= is rejected at decoration."""
    app = Veloce()
    with pytest.raises(ValueError, match="exactly one"):

        @app.mcp_completer(argument="name")
        async def neither(value, context):
            return []

    with pytest.raises(ValueError, match="exactly one"):

        @app.mcp_completer(prompt="a", resource="b://c", argument="name")
        async def both(value, context):
            return []


def test_completions_capability_is_slotted():
    """The completions capability stays slotted, gaining no per-instance dict."""
    app = Veloce()
    cap = CompletionsCapability(_server(app))
    assert not hasattr(cap, "__dict__")


# ── a completer is gated by the primitive that owns it ───────────────
#
# `completion/complete` invoked the completer for anyone. Every other
# invocation path - `tools/call`, `resources/read`, `prompts/get` - calls
# `_principal_lacks_scopes` first, and `docs/guide/mcp.md` states the rule for
# all of them: "Anything a caller must be refused needs scopes=, which is
# checked on every call." A completer's whole purpose is to enumerate the legal
# values of an argument - customer ids, tenant names, file paths - so an
# under-scoped caller could enumerate the key space of a primitive it may not
# read, narrowing it by prefix through `argument.value`, and run the
# application code behind it.


def _scoped_prompt_server(scopes: list[str]) -> MCPServer:
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(name="vault", description="Open the vault.", scopes=scopes)
    async def vault(request, account: str):
        return f"opening {account}"

    @app.mcp_completer(prompt="vault", argument="account")
    async def complete_account(value: str, context: dict):
        return CompletionResult(values=["acct-1001", "acct-1002"])

    return MCPServer(app)


async def test_an_under_scoped_caller_gets_no_completions(monkeypatch):
    """NEGATIVE: the completer must not run for a principal lacking the scopes."""
    set_principal(Principal(subject="attacker", scopes=frozenset({"nothing"})))
    try:
        server = _scoped_prompt_server(["prompts:use"])
        result = await _complete(server, {"type": "ref/prompt", "name": "vault"}, "account", "acct")
    finally:
        set_principal(None)

    assert result["completion"]["values"] == []


async def test_an_under_scoped_caller_cannot_narrow_by_prefix():
    """NEGATIVE: the refusal must not depend on the partial value.

    Prefix narrowing is how a key space gets enumerated, so an empty prefix and
    a specific one must both yield nothing.
    """
    set_principal(Principal(subject="attacker", scopes=frozenset()))
    try:
        server = _scoped_prompt_server(["prompts:use"])
        for value in ("", "acct-10", "acct-1001"):
            result = await _complete(
                server, {"type": "ref/prompt", "name": "vault"}, "account", value
            )
            assert result["completion"]["values"] == [], value
    finally:
        set_principal(None)


async def test_a_correctly_scoped_caller_still_gets_completions():
    """POSITIVE: the gate must not break the feature for an allowed principal."""
    set_principal(Principal(subject="ok", scopes=frozenset({"prompts:use"})))
    try:
        server = _scoped_prompt_server(["prompts:use"])
        result = await _complete(server, {"type": "ref/prompt", "name": "vault"}, "account", "acct")
    finally:
        set_principal(None)

    assert result["completion"]["values"] == ["acct-1001", "acct-1002"]


async def test_an_unscoped_prompt_completes_without_a_principal():
    """POSITIVE: a prompt declaring no scopes is open, as it always was."""
    server = _scoped_prompt_server([])

    result = await _complete(server, {"type": "ref/prompt", "name": "vault"}, "account", "acct")

    assert result["completion"]["values"] == ["acct-1001", "acct-1002"]
