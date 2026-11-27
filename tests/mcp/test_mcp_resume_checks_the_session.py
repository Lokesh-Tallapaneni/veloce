"""A resuming `GET` is refused once its session is gone.

`_handle_get` read only `Last-Event-ID`. It never looked at `Mcp-Session-Id` and
never consulted the session store, and `SSEEventStore` has no session dimension -
so under `sessions=True, resumable=True` a client whose session had been
`DELETE`d, or evicted by the idle TTL or the `max_sessions` reclaim, could still
`GET` and be handed that stream's buffered JSON-RPC payloads, tool results
included.

`DELETE` next door already answered 404 for the same id. The two verbs disagreed
about whether a terminated session still exists.

The MCP transport requires a request carrying a terminated session id to be
answered 404, which is what `_bind_session` already produces for `POST`. The
resume goes through the same gate.
"""

from __future__ import annotations

import orjson

from veloce import AsyncTestClient, Veloce

BOTH = "application/json, text/event-stream"

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"capabilities": {}, "clientInfo": {"name": "probe", "version": "1"}},
}


def _app(**kwargs) -> Veloce:
    app = Veloce(title="Resumable", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", **kwargs)
    return app


async def _open(client) -> tuple[str, str]:
    """Initialize, then run one call so the event store holds a replayable id."""
    opened = await client.post(
        "/mcp",
        json=_INIT,
        headers={"accept": BOTH, "content-type": "application/json"},
    )
    session_id = opened.headers["mcp-session-id"]
    last_event_id = ""
    for line in opened.body.decode().splitlines():
        if line.startswith("id: "):
            last_event_id = line[4:].strip()
    return session_id, last_event_id


async def test_a_resume_with_a_live_session_is_served():
    """The control: resumability must still work."""
    async with AsyncTestClient(_app(sessions=True, resumable=True)) as client:
        session_id, last_event_id = await _open(client)

        resumed = await client.get(
            "/mcp",
            headers={
                "accept": "text/event-stream",
                "last-event-id": last_event_id,
                "mcp-session-id": session_id,
            },
        )

    assert resumed.status_code == 200


async def test_a_resume_after_delete_is_refused():
    """The regression: the stream replayed for a session the server had dropped."""
    async with AsyncTestClient(_app(sessions=True, resumable=True)) as client:
        session_id, last_event_id = await _open(client)

        ended = await client.delete("/mcp", headers={"mcp-session-id": session_id})
        assert ended.status_code == 204

        resumed = await client.get(
            "/mcp",
            headers={
                "accept": "text/event-stream",
                "last-event-id": last_event_id,
                "mcp-session-id": session_id,
            },
        )

    assert resumed.status_code == 404, "a terminated session could still replay its payloads"


async def test_a_resume_after_delete_carries_no_payloads():
    """The refusal has to withhold the data, not merely change the status."""
    async with AsyncTestClient(_app(sessions=True, resumable=True)) as client:
        session_id, last_event_id = await _open(client)
        await client.delete("/mcp", headers={"mcp-session-id": session_id})

        resumed = await client.get(
            "/mcp",
            headers={
                "accept": "text/event-stream",
                "last-event-id": last_event_id,
                "mcp-session-id": session_id,
            },
        )

    assert b"protocolVersion" not in resumed.body


async def test_a_resume_with_an_unknown_session_is_refused():
    """An id the server never minted is the same case as a terminated one."""
    async with AsyncTestClient(_app(sessions=True, resumable=True)) as client:
        _session_id, last_event_id = await _open(client)

        resumed = await client.get(
            "/mcp",
            headers={
                "accept": "text/event-stream",
                "last-event-id": last_event_id,
                "mcp-session-id": "never-minted",
            },
        )

    assert resumed.status_code == 404


async def test_a_resume_with_no_session_header_is_refused():
    """Under session management every non-initialize request must name one."""
    async with AsyncTestClient(_app(sessions=True, resumable=True)) as client:
        _session_id, last_event_id = await _open(client)

        resumed = await client.get(
            "/mcp", headers={"accept": "text/event-stream", "last-event-id": last_event_id}
        )

    assert resumed.status_code == 400


async def test_the_refusal_is_a_json_rpc_error():
    """`POST` and `DELETE` answer this shape; `GET` must not invent another."""
    async with AsyncTestClient(_app(sessions=True, resumable=True)) as client:
        session_id, last_event_id = await _open(client)
        await client.delete("/mcp", headers={"mcp-session-id": session_id})

        resumed = await client.get(
            "/mcp",
            headers={
                "accept": "text/event-stream",
                "last-event-id": last_event_id,
                "mcp-session-id": session_id,
            },
        )

    assert orjson.loads(resumed.body)["error"]["code"]


async def test_resumability_without_sessions_is_unaffected():
    """No session management means no session to check, and no new refusal."""
    async with AsyncTestClient(_app(resumable=True)) as client:
        opened = await client.post(
            "/mcp",
            json=_INIT,
            headers={"accept": BOTH, "content-type": "application/json"},
        )
        last_event_id = ""
        for line in opened.body.decode().splitlines():
            if line.startswith("id: "):
                last_event_id = line[4:].strip()

        resumed = await client.get(
            "/mcp", headers={"accept": "text/event-stream", "last-event-id": last_event_id}
        )

    assert resumed.status_code == 200


async def test_a_get_without_resumability_is_still_405():
    """The other reason a `GET` is refused must keep its own status."""
    async with AsyncTestClient(_app(sessions=True)) as client:
        answered = await client.get("/mcp", headers={"accept": "text/event-stream"})

    assert answered.status_code == 405
