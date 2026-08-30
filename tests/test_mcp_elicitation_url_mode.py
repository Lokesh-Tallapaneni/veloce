"""URL-mode elicitation carries what the spec requires, and only when it may.

Two rules govern a `mode: "url"` elicitation request:

- it MUST carry an `elicitationId`, which names the interaction so a later
  `notifications/elicitation/complete` can be matched to it. It is a required
  field on `ElicitRequestURLParams`, so a client validating the request against
  the published types rejects one without it;
- the server MUST NOT send it at all unless the client declared it supports URL
  mode. An `elicitation` capability with no `url` key - including the empty
  `elicitation: {}`, which means form-only - is not a declaration.

Form mode is deliberately not gated the same way: an empty `elicitation: {}` is
exactly what a form-only client sends.
"""

from __future__ import annotations

import pytest

from veloce import MCPContext, Veloce
from veloce.contrib.mcp._helpers import _requester_var
from veloce.contrib.mcp.errors import MCPCapabilityError
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession

URL = "https://mcp.example.com/authorize"


def _context(capabilities: dict, sent: list) -> MCPContext:
    """A context wired to a fake bidirectional transport that records requests."""

    async def requester(method: str, params: dict) -> dict:
        sent.append((method, params))
        return {"action": "accept"}

    return MCPContext("probe", requester=requester, client_capabilities=capabilities)


# ── The required identifier ──────────────────────────────────────────


async def test_a_url_request_carries_an_elicitation_id():
    sent: list = []
    await _context({"elicitation": {"url": {}}}, sent).elicit("Authorize", url=URL)
    _method, params = sent[0]
    assert params["elicitationId"]
    assert isinstance(params["elicitationId"], str)


async def test_the_request_carries_the_mode_and_url_the_spec_names():
    sent: list = []
    await _context({"elicitation": {"url": {}}}, sent).elicit("Authorize", url=URL)
    method, params = sent[0]
    assert method == "elicitation/create"
    assert params["mode"] == "url"
    assert params["url"] == URL
    assert params["message"] == "Authorize"


async def test_each_call_gets_its_own_identifier():
    """Two interactions must be distinguishable when either completes."""
    sent: list = []
    context = _context({"elicitation": {"url": {}}}, sent)
    await context.elicit("first", url=URL)
    await context.elicit("second", url=URL)
    assert sent[0][1]["elicitationId"] != sent[1][1]["elicitationId"]


async def test_a_caller_may_supply_its_own_identifier():
    """The URL flow often already has an id the completion will be reported under."""
    sent: list = []
    await _context({"elicitation": {"url": {}}}, sent).elicit(
        "Authorize", url=URL, elicitation_id="flow-42"
    )
    assert sent[0][1]["elicitationId"] == "flow-42"


async def test_a_minted_identifier_is_not_guessable():
    sent: list = []
    await _context({"elicitation": {"url": {}}}, sent).elicit("Authorize", url=URL)
    assert len(sent[0][1]["elicitationId"]) >= 16


# ── Only a client that declared URL mode receives one ────────────────


@pytest.mark.parametrize(
    "capabilities",
    [
        pytest.param({"elicitation": {}}, id="empty means form-only"),
        pytest.param({"elicitation": {"form": {}}}, id="form declared, url not"),
    ],
)
async def test_url_mode_is_refused_when_the_client_did_not_declare_it(capabilities: dict):
    sent: list = []
    with pytest.raises(MCPCapabilityError):
        await _context(capabilities, sent).elicit("Authorize", url=URL)
    assert sent == [], "nothing may go on the wire"


async def test_the_refusal_names_the_missing_capability():
    with pytest.raises(MCPCapabilityError, match="elicitation.url"):
        await _context({"elicitation": {"form": {}}}, []).elicit("Authorize", url=URL)


async def test_a_client_declaring_no_elicitation_at_all_is_still_refused():
    with pytest.raises(MCPCapabilityError):
        await _context({}, []).elicit("Authorize", url=URL)


# ── Form mode is unaffected ──────────────────────────────────────────


async def test_form_mode_works_for_a_client_declaring_only_the_empty_capability():
    """`elicitation: {}` is what a form-only client sends; it must still work."""
    sent: list = []
    await _context({"elicitation": {}}, sent).elicit(
        "Your name?", requested_schema={"type": "object", "properties": {"name": {}}}
    )
    _method, params = sent[0]
    assert params["requestedSchema"] == {"type": "object", "properties": {"name": {}}}
    assert "mode" not in params
    assert "elicitationId" not in params, "the id belongs to URL mode only"


async def test_form_mode_is_not_gated_on_a_form_sub_capability():
    sent: list = []
    await _context({"elicitation": {"url": {}}}, sent).elicit(
        "Your name?", requested_schema={"type": "object"}
    )
    assert sent, "a form request must still be sent"


async def test_asking_for_both_modes_at_once_is_refused():
    with pytest.raises(ValueError, match="either requested_schema"):
        await _context({"elicitation": {"url": {}}}, []).elicit(
            "Pick one", requested_schema={"type": "object"}, url=URL
        )


# ── Off a bidirectional transport ────────────────────────────────────


async def test_elicit_still_reports_a_transport_that_cannot_carry_it():
    """The HTTP transport has no server-to-client request channel."""
    context = MCPContext("probe")
    context._client_capabilities = {"elicitation": {"url": {}}}
    with pytest.raises(RuntimeError, match="bidirectional"):
        await context.elicit("Authorize", url=URL)


# ── Through a real tool call ─────────────────────────────────────────


async def test_a_tool_eliciting_a_url_sends_a_conforming_request():
    """End to end through the dispatcher, not just the context object."""
    app = Veloce(title="Elicit", openapi_url=None)

    @app.mcp_tool(description="Ask the user to authorize")
    async def authorize(ctx: MCPContext) -> dict:
        return await ctx.elicit("Authorize this app", url=URL)

    sent: list = []

    async def requester(method: str, params: dict) -> dict:
        sent.append((method, params))
        return {"action": "accept"}

    session = MCPSession()
    session.record_initialize({"capabilities": {"elicitation": {"url": {}}}})

    # The transports publish their server-to-client channel here; a test stands in
    # for one rather than opening a real stdio pipe.
    token = _requester_var.set(requester)
    try:
        response = await MCPServer(app).handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "authorize", "arguments": {}},
            },
            session,
        )
    finally:
        _requester_var.reset(token)

    assert "error" not in response
    assert not response["result"].get("isError"), response["result"]["content"][0]["text"]
    _method, params = sent[0]
    assert set(params) >= {"message", "mode", "url", "elicitationId"}
    assert params["mode"] == "url"
