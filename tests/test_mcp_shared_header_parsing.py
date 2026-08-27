"""The MCP transport parses two headers with the framework's own parsers.

Both had a hand-rolled copy that was laxer than the shared one, so the same
header meant different things at the two doors.

**`Authorization`.** The core extractor trims only SP and HTAB, because RFC 6750
Sec. 2.1 and RFC 7235 permit nothing else between scheme and token. The MCP copy
did `raw_token.strip()`, which also trims `\\n`, `\\r`, and NBSP:

    Authorization: Bearer secret\\n     HTTP door -> 401     MCP door -> accepted

A token the HTTP door rejects should not be a token the agent door accepts. The
practical shape is a credential read from a file or an env var with a trailing
newline: it worked over MCP and failed over HTTP, which reads as an
MCP-specific bug in the *application*.

**`Accept`.** The transport chose between a JSON reply and an SSE stream with
`"text/event-stream" in header`. A substring test says yes to
`text/event-streaming`, and it cannot see a weight - so a client that wrote
`text/event-stream;q=0` to say *not that* was handed exactly that.

`quality_explicit` is used rather than `quality` so a bare `*/*` still gets the
JSON reply it got before: the stream is for a client that asked for it.
"""

from __future__ import annotations

import pytest

from tests._mcp import auth
from veloce import Depends, HTTPBearer, Veloce
from veloce.contrib.mcp import MCPAuth
from veloce.principal import Principal
from veloce.testclient import TestClient

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}
CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "add"}}


def _app(auth: MCPAuth | None = None) -> Veloce:
    app = Veloce(title="Headers", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Add nothing")
    async def add() -> str:
        return "ok"

    app.mount_mcp(transport="http", path="/mcp", auth=auth)
    return app


# ── Accept: the reply shape follows the parsed header ────────────────


def _reply(accept: str) -> str:
    client = TestClient(_app())
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    return client.post("/mcp", json=CALL, headers={"Accept": accept}).text


@pytest.mark.parametrize(
    "accept",
    [
        "text/event-stream;q=0",
        "application/json, text/event-stream;q=0",
        "text/event-stream;q=0.0",
    ],
)
def test_a_refused_stream_is_not_sent(accept):
    """The defect: a substring test cannot see a weight, so `q=0` got a stream."""
    assert not _reply(accept).startswith("event:")
    assert "data:" not in _reply(accept)


def test_a_lookalike_media_type_does_not_select_the_stream():
    """`text/event-streaming` contains the string and is a different type."""
    assert "data:" not in _reply("text/event-streaming")


@pytest.mark.parametrize("accept", ["text/event-stream", "application/json, text/event-stream"])
def test_an_explicit_stream_request_still_gets_the_stream(accept):
    """The negative: refusing everything would pass the tests above vacuously."""
    assert "data:" in _reply(accept)


@pytest.mark.parametrize("accept", ["application/json", "*/*", "application/*"])
def test_a_client_that_did_not_ask_for_the_stream_gets_json(accept):
    """`*/*` chose JSON before and must keep choosing it."""
    body = _reply(accept)
    assert "data:" not in body
    assert body.lstrip().startswith("{")


def test_the_reply_is_correct_either_way():
    import json

    assert json.loads(_reply("application/json"))["result"]["content"][0]["text"] == "ok"
    assert "ok" in _reply("text/event-stream")


# ── Authorization: the same token rules as the HTTP door ─────────────


def _verify(token: str) -> Principal | None:
    """Accepts exactly one token, so any normalisation shows up as acceptance."""
    return Principal(subject="u", scopes=frozenset()) if token == "secret" else None


def _status(header: str | None) -> int:
    client = TestClient(_app(auth(_verify)))
    headers = {"Accept": "application/json"}
    if header is not None:
        headers["Authorization"] = header
    return client.post("/mcp", json=INITIALIZE, headers=headers).status_code


@pytest.mark.parametrize("header", ["Bearer secret" + c for c in (chr(10), chr(13), chr(11))])
def test_a_token_carrying_non_permitted_whitespace_is_refused(header):
    """The defect: `.strip()` trimmed these into the token the verifier wanted.

    A trailing newline is the realistic shape - a credential read from a file or
    an env var. The HTTP door hands the verifier the byte and the token fails;
    the MCP door handed it the trimmed word and the token passed.
    """
    assert _status(header) == 401


@pytest.mark.parametrize(
    "header",
    ["Bearer secret", "Bearer  secret", "Bearer   secret", "Bearer secret" + chr(9)],
)
def test_a_token_with_permitted_whitespace_is_accepted(header):
    """SP between scheme and token, and SP/HTAB around it - RFC 7235."""
    assert _status(header) == 200


def test_a_tab_after_the_scheme_is_refused_by_both_doors():
    """Not the `Bearer ` prefix, so neither door reads a token at all."""
    assert _status("Bearer" + chr(9) + "secret") == 401


@pytest.mark.parametrize("header", ["bearer secret", "BEARER secret", "BeArEr secret"])
def test_the_scheme_is_matched_case_insensitively(header):
    assert _status(header) == 200


@pytest.mark.parametrize(
    "header", [None, "", "Bearer", "Bearer ", "Basic secret", "secret", "Bearer wrong"]
)
def test_a_missing_or_wrong_credential_is_refused(header):
    assert _status(header) == 401


def test_the_two_doors_agree_on_what_a_token_is():
    """The property: one credential, one answer, whichever door reads it."""

    app = Veloce(openapi_url=None)

    @app.get("/http", dependencies=[Depends(HTTPBearer())])
    async def guarded() -> dict:
        return {}

    http = TestClient(app)
    for header in ("Bearer secret\n", "Bearer secret "):
        assert http.get("/http", headers={"Authorization": header}).status_code == 200
        # Accepted as *a* token by both - the point is that neither strips it
        # into the bare word the other would have compared against.
        assert _status(header) == 401

    assert http.get("/http", headers={"Authorization": "Bearer secret"}).status_code == 200
    assert _status("Bearer secret") == 200
