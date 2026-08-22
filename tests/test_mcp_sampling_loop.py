"""Driving the client's LLM through tool calls until it answers.

`sample()` is one round trip: a prompt out, a completion back. The modern
revision lets that request carry tools, and lets the model answer with a request
to call one - leaving the handler to execute it, append the result and ask again,
round after round, before it has an answer.

`sample_with_tools` runs that loop over tools this server already has, and hands
back what happened: the answer, the transcript, and every call made.
"""

from __future__ import annotations

import orjson
import pytest

from veloce import MCPContext, Veloce
from veloce.contrib.mcp._helpers import _requester_var
from veloce.contrib.mcp.errors import MCPCapabilityError
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession

CAPABLE = {"sampling": {"tools": {}}}


def _text(text: str, stop: str = "endTurn") -> dict:
    return {
        "model": "m",
        "role": "assistant",
        "stopReason": stop,
        "content": {"type": "text", "text": text},
    }


def _uses(*calls: tuple[str, str, dict]) -> dict:
    return {
        "model": "m",
        "role": "assistant",
        "stopReason": "toolUse",
        "content": [
            {"type": "tool_use", "name": name, "id": use_id, "input": arguments}
            for name, use_id, arguments in calls
        ],
    }


class Client:
    """A client answering each sampling request from a scripted list."""

    def __init__(self, *answers: dict) -> None:
        self.answers = list(answers)
        self.requests: list[dict] = []

    async def __call__(self, method: str, params: dict) -> dict:
        self.requests.append(params)
        if len(self.answers) > 1:
            return self.answers.pop(0)
        return self.answers[0]


def _app(**loop_kwargs) -> Veloce:
    app = Veloce(title="Loop", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Look up a city's weather")
    async def weather(city: str) -> dict:
        return {"city": city, "c": 21}

    @app.mcp_tool(description="Always fails")
    async def boom() -> str:
        raise ValueError("no")

    @app.mcp_tool(description="Needs a grant", scopes=["admin"])
    async def privileged() -> str:
        return "secret"

    @app.mcp_tool(description="Not offered to the model")
    async def unoffered() -> str:
        return "nope"

    @app.mcp_tool(description="Answer a question")
    async def ask(question: str, ctx: MCPContext) -> dict:
        run = await ctx.sample_with_tools(
            [{"role": "user", "content": {"type": "text", "text": question}}],
            tools=loop_kwargs.pop("tools", ["weather"]),
            max_tokens=256,
            **loop_kwargs,
        )
        return {
            "text": run.text,
            "rounds": run.rounds,
            "stop_reason": run.stop_reason,
            "model": run.model,
            "messages": list(run.messages),
            "calls": [
                {"name": c.name, "id": c.id, "arguments": c.arguments, "isError": c.is_error}
                for c in run.tool_calls
            ],
        }

    return app


async def _run(app: Veloce, client: Client, capabilities: dict = CAPABLE) -> dict:
    session = MCPSession()
    session.client_capabilities = capabilities
    token = _requester_var.set(client)
    try:
        response = await MCPServer(app).handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ask", "arguments": {"question": "q"}},
            },
            session,
        )
    finally:
        _requester_var.reset(token)
    return response["result"]


async def _ask(app: Veloce, client: Client) -> dict:
    result = await _run(app, client)
    assert not result.get("isError"), result["content"][0]["text"]
    parsed: dict = orjson.loads(result["content"][0]["text"])
    return parsed


# ── Without tool calls ───────────────────────────────────────────────


async def test_a_plain_answer_ends_the_run_in_one_round():
    answer = await _ask(_app(), Client(_text("It is 21C.")))
    assert (answer["text"], answer["rounds"]) == ("It is 21C.", 1)


async def test_the_model_and_stop_reason_are_reported():
    answer = await _ask(_app(), Client(_text("done")))
    assert (answer["model"], answer["stop_reason"]) == ("m", "endTurn")


async def test_content_sent_as_a_single_block_reads_the_same_as_a_list():
    """The spec allows either shape; a caller should not have to care which."""
    listed = {"model": "m", "role": "assistant", "content": [{"type": "text", "text": "hi"}]}
    assert (await _ask(_app(), Client(listed)))["text"] == "hi"


async def test_several_text_blocks_join():
    listed = {
        "model": "m",
        "role": "assistant",
        "content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
    }
    assert (await _ask(_app(), Client(listed)))["text"] == "one\ntwo"


# ── Executing what the model asks for ────────────────────────────────


async def test_a_requested_tool_runs_and_the_run_continues():
    client = Client(_uses(("weather", "t1", {"city": "Nuremberg"})), _text("It is 21C."))
    answer = await _ask(_app(), client)
    assert answer["text"] == "It is 21C."
    assert answer["rounds"] == 2


async def test_the_call_is_reported_with_what_it_was_given():
    client = Client(_uses(("weather", "t1", {"city": "Nuremberg"})), _text("ok"))
    assert (await _ask(_app(), client))["calls"][0] == {
        "name": "weather",
        "id": "t1",
        "arguments": {"city": "Nuremberg"},
        "isError": False,
    }


async def test_the_result_is_fed_back_as_the_next_message():
    client = Client(_uses(("weather", "t1", {"city": "Nuremberg"})), _text("ok"))
    await _ask(_app(), client)
    fed = client.requests[1]["messages"][-1]
    assert fed["role"] == "user"
    assert fed["content"][0]["type"] == "tool_result"
    assert fed["content"][0]["toolUseId"] == "t1"
    assert "Nuremberg" in fed["content"][0]["content"][0]["text"]


async def test_the_request_is_echoed_back_as_the_assistant_turn():
    """Without it the transcript holds a result answering nothing."""
    client = Client(_uses(("weather", "t1", {"city": "Berlin"})), _text("ok"))
    await _ask(_app(), client)
    echoed = client.requests[1]["messages"][1]
    assert echoed["role"] == "assistant"
    assert echoed["content"][0]["type"] == "tool_use"


async def test_two_requests_in_one_turn_both_run():
    client = Client(
        _uses(("weather", "t1", {"city": "A"}), ("weather", "t2", {"city": "B"})),
        _text("ok"),
    )
    calls = (await _ask(_app(), client))["calls"]
    assert [call["arguments"]["city"] for call in calls] == ["A", "B"]
    assert len(client.requests[1]["messages"][-1]["content"]) == 2


async def test_the_transcript_is_returned_for_a_later_run():
    client = Client(_uses(("weather", "t1", {"city": "A"})), _text("ok"))
    messages = (await _ask(_app(), client))["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user"]


async def test_each_request_carries_the_transcript_as_it_stood():
    """A later append must not reach a request already sent."""
    client = Client(_uses(("weather", "t1", {"city": "A"})), _text("ok"))
    await _ask(_app(), client)
    assert [len(params["messages"]) for params in client.requests] == [1, 3]


async def test_the_declared_tools_travel_with_every_request():
    client = Client(_uses(("weather", "t1", {"city": "A"})), _text("ok"))
    await _ask(_app(), client)
    for params in client.requests:
        assert [tool["name"] for tool in params["tools"]] == ["weather"]


async def test_the_declaration_carries_the_schema_the_tool_publishes():
    client = Client(_text("ok"))
    await _ask(_app(), client)
    declared = client.requests[0]["tools"][0]
    assert declared["inputSchema"]["properties"]["city"]["type"] == "string"
    assert declared["description"] == "Look up a city's weather"


# ── Only the offered tools ───────────────────────────────────────────


async def test_a_tool_outside_the_offered_set_is_not_executed():
    """The handler chose what the model may drive; that has to be a restriction."""
    client = Client(_uses(("unoffered", "t1", {})), _text("ok"))
    assert (await _ask(_app(), client))["calls"][0]["isError"] is True
    relayed = client.requests[1]["messages"][-1]["content"][0]["content"][0]["text"]
    assert "unoffered" in relayed


async def test_a_tool_that_does_not_exist_is_refused_the_same_way():
    client = Client(_uses(("invented", "t1", {})), _text("ok"))
    assert (await _ask(_app(), client))["calls"][0]["isError"] is True


async def test_offering_a_tool_the_server_does_not_have_is_refused_up_front():
    result = await _run(_app(tools=["weather", "typo"]), Client(_text("ok")))
    assert result["isError"] is True


async def test_that_refusal_names_the_tool_and_what_does_exist():
    context = MCPContext("bare", server=MCPServer(_app()))
    with pytest.raises(ValueError, match="typo.*weather"):
        await context.sample_with_tools([], tools=["typo"], max_tokens=1)


# ── Failures reach the model, not the handler ────────────────────────


async def test_a_failing_tool_is_reported_and_the_run_continues():
    """The model asked for this call and can correct itself given the reason."""
    client = Client(_uses(("boom", "t1", {})), _text("I could not."))
    answer = await _ask(_app(tools=["weather", "boom"]), client)
    assert answer["calls"][0]["isError"] is True
    assert answer["text"] == "I could not."


async def test_a_tool_the_caller_may_not_invoke_is_not_invoked():
    """Scopes are enforced here exactly as on a direct `tools/call`."""
    client = Client(_uses(("privileged", "t1", {})), _text("denied"))
    answer = await _ask(_app(tools=["privileged"]), client)
    assert answer["calls"][0]["isError"] is True
    assert answer["text"] == "denied"


async def test_missing_arguments_come_back_as_an_error_result():
    client = Client(_uses(("weather", "t1", {})), _text("retrying"))
    assert (await _ask(_app(), client))["calls"][0]["isError"] is True


# ── The round cap ────────────────────────────────────────────────────


async def test_the_loop_stops_at_the_cap():
    """A model that keeps asking must not keep the handler running forever."""
    client = Client(_uses(("weather", "t1", {"city": "A"})))
    answer = await _ask(_app(max_tool_rounds=2), client)
    assert answer["rounds"] == 3
    assert len(answer["calls"]) == 2


async def test_the_last_round_asks_for_an_answer_rather_than_a_tool():
    client = Client(_uses(("weather", "t1", {"city": "A"})))
    await _ask(_app(max_tool_rounds=1), client)
    assert client.requests[0].get("toolChoice") is None
    assert client.requests[1]["toolChoice"] == {"mode": "none"}


async def test_a_cap_of_zero_never_executes_a_tool():
    client = Client(_uses(("weather", "t1", {"city": "A"})))
    answer = await _ask(_app(max_tool_rounds=0), client)
    assert answer["calls"] == []
    assert answer["rounds"] == 1


async def test_the_callers_tool_choice_travels_until_the_last_round():
    client = Client(_uses(("weather", "t1", {"city": "A"})))
    await _ask(_app(max_tool_rounds=1, tool_choice={"mode": "required"}), client)
    assert client.requests[0]["toolChoice"] == {"mode": "required"}


# ── What the loop needs ──────────────────────────────────────────────


async def test_a_client_without_the_tools_capability_is_refused():
    result = await _run(_app(), Client(_text("ok")), capabilities={"sampling": {}})
    assert result["isError"] is True


async def test_a_bare_context_says_what_it_is_missing():
    context = MCPContext("bare")
    with pytest.raises(RuntimeError, match="sample_with_tools"):
        await context.sample_with_tools([], tools=[], max_tokens=1)


async def test_the_capability_is_checked_by_the_underlying_request():
    context = MCPContext("bare", client_capabilities={"sampling": {}})
    with pytest.raises(MCPCapabilityError):
        await context.sample([], max_tokens=1, tools=[])
