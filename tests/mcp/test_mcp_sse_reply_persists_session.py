"""An SSE-answered request publishes its session, exactly as a JSON-answered one does.

A spec-conformant client MUST offer both `application/json` and `text/event-stream`
on its POST, and `_needs_stream` keeps the stream for every method other than
`tools/call`. So `initialize` - the one message that establishes the whole
session - is answered as SSE for every real client.

That path returned the stream before reaching `store.persist`, so the record
written to the backend carried `client_capabilities={}` and `client_info=None`.
It compounds: `HttpSessionStore.resolve` overwrites the live session's lifecycle
fields from the stored record, so the empty record wiped the handshake even on
the worker that ran it, and the next call re-persisted the wiped state.

Downstream, `ctx.sample()` / `ctx.elicit()` / `ctx.roots()` raise
`MCPCapabilityError` for a client that did advertise the capability,
`MCPContext.client_info` is `None`, and under `MCP_ENFORCE_LIFECYCLE` a second
worker rejects every request as pre-initialization.

The existing backend tests only ever send `accept: application/json`, which is
the one shape a real client does not send.
"""

from __future__ import annotations

from typing import Any

import orjson

from veloce import AsyncTestClient, Veloce
from veloce.contrib.mcp.transports.session_store import SessionRecord

#: What a spec-conformant client sends: both types, stream preferred where offered.
BOTH = "application/json, text/event-stream"

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "capabilities": {"sampling": {}, "elicitation": {}},
        "clientInfo": {"name": "probe", "version": "9.9"},
    },
}
_CALL = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
}


class MemoryBackend:
    """A backend two stores can share, standing in for Redis in a test."""

    def __init__(self) -> None:
        self.records: dict[str, SessionRecord] = {}

    async def read(self, session_id: str) -> SessionRecord | None:
        return self.records.get(session_id)

    async def write(self, session_id: str, record: SessionRecord, ttl: int) -> None:
        self.records[session_id] = record

    async def delete(self, session_id: str) -> bool:
        return self.records.pop(session_id, None) is not None


def _app() -> Veloce:
    app = Veloce(title="Shared", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    return app


async def _post(client: Any, body: dict, accept: str, session_id: str | None = None) -> Any:
    headers = {"accept": accept, "content-type": "application/json"}
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return await client.post("/mcp", json=body, headers=headers)


async def test_an_sse_answered_initialize_publishes_the_handshake():
    """The regression: the record was written before `initialize` ran."""
    backend = MemoryBackend()
    app = _app()
    app.mount_mcp(transport="http", sessions=True, session_backend=backend)

    async with AsyncTestClient(app) as client:
        opened = await _post(client, _INIT, BOTH)
        session_id = opened.headers["mcp-session-id"]

    # `initialized` flips on `notifications/initialized`, not here - `initialize`
    # establishes the capabilities and the identity, which is what was being lost.
    record = backend.records[session_id]
    assert record.client_capabilities == {"sampling": {}, "elicitation": {}}
    assert record.client_info == {"name": "probe", "version": "9.9"}


async def test_a_json_answered_initialize_publishes_the_same_thing():
    """The control: this path always worked, and is what the other must match."""
    backend = MemoryBackend()
    app = _app()
    app.mount_mcp(transport="http", sessions=True, session_backend=backend)

    async with AsyncTestClient(app) as client:
        opened = await _post(client, _INIT, "application/json")
        session_id = opened.headers["mcp-session-id"]

    record = backend.records[session_id]
    assert record.client_capabilities == {"sampling": {}, "elicitation": {}}
    assert record.client_info == {"name": "probe", "version": "9.9"}


async def test_both_doors_publish_the_same_record():
    """Stated as one assertion, because the defect was the two disagreeing."""
    streamed, plain = MemoryBackend(), MemoryBackend()
    sse_app, json_app = _app(), _app()
    sse_app.mount_mcp(transport="http", sessions=True, session_backend=streamed)
    json_app.mount_mcp(transport="http", sessions=True, session_backend=plain)

    async with AsyncTestClient(sse_app) as a, AsyncTestClient(json_app) as b:
        sse_id = (await _post(a, _INIT, BOTH)).headers["mcp-session-id"]
        json_id = (await _post(b, _INIT, "application/json")).headers["mcp-session-id"]

    assert streamed.records[sse_id] == plain.records[json_id]


async def test_the_handshake_survives_a_later_call_on_the_same_worker():
    """`resolve` overwrites live state from the record, so a bad record wipes it."""
    backend = MemoryBackend()
    app = _app()
    app.mount_mcp(transport="http", sessions=True, session_backend=backend)

    async with AsyncTestClient(app) as client:
        session_id = (await _post(client, _INIT, BOTH)).headers["mcp-session-id"]
        served = await _post(client, _CALL, "application/json", session_id)
        assert served.status_code == 200

    record = backend.records[session_id]
    assert record.client_info == {"name": "probe", "version": "9.9"}, (
        "the follow-up call re-persisted a wiped record"
    )


async def test_a_second_worker_sees_the_sse_handshake():
    """What the backend exists for, reached through the accept header clients send."""
    backend = MemoryBackend()
    worker_a, worker_b = _app(), _app()
    worker_a.mount_mcp(transport="http", sessions=True, session_backend=backend)
    worker_b.mount_mcp(transport="http", sessions=True, session_backend=backend)

    async with AsyncTestClient(worker_a) as a, AsyncTestClient(worker_b) as b:
        session_id = (await _post(a, _INIT, BOTH)).headers["mcp-session-id"]

        served = await _post(b, _CALL, "application/json", session_id)
        assert served.status_code == 200
        assert orjson.loads(served.body)["result"]["content"][0]["text"] == "5"


async def test_the_stream_still_carries_the_initialize_response():
    """Persisting must not cost the reply the stream exists to deliver."""
    backend = MemoryBackend()
    app = _app()
    app.mount_mcp(transport="http", sessions=True, session_backend=backend)

    async with AsyncTestClient(app) as client:
        opened = await _post(client, _INIT, BOTH)

    assert opened.status_code == 200
    assert "text/event-stream" in opened.headers["content-type"]
    assert b'"protocolVersion"' in opened.body


async def test_a_streamed_reply_without_a_backend_still_works():
    """The no-backend path is the common one and must stay a no-op, not a crash."""
    app = _app()
    app.mount_mcp(transport="http", sessions=True)

    async with AsyncTestClient(app) as client:
        opened = await _post(client, _INIT, BOTH)
        session_id = opened.headers["mcp-session-id"]
        served = await _post(client, _CALL, "application/json", session_id)

    assert opened.status_code == 200
    assert served.status_code == 200
