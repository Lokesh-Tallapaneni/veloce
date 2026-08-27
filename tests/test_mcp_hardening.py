"""Origin checks, the single `is_mcp` test, metadata, and provenance.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import asyncio

import orjson
import pytest

from tests._mcp_shared import (
    _auth,
    _call,
    _mcp_call_body,
    _parse_sse,
    _sse_event_ids,
    _verify,
)
from veloce import (
    JSONResponse,
    MCPContext,
    Middleware,
    Principal,
    Request,
    Veloce,
)
from veloce.contrib.mcp import MCPAuth
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.tasks import STATUS_CANCELLED, STATUS_COMPLETED, new_task
from veloce.contrib.mcp.transports.event_store import SSEEventStore
from veloce.contrib.mcp.transports.http import _stream_response

# -- Hardening: single is_mcp check, Origin, metadata, provenance -----


def test_exclude_middleware_and_is_mcp_cover_transport_and_replay():
    app = Veloce(openapi_url=None)

    class Auth(Middleware):
        async def process_request(self, request):
            if request.is_mcp:  # replayed tool call → transport already authed
                return None
            return JSONResponse({"detail": "no"}, status_code=401)

    app.add_middleware(Auth)

    @app.get("/order/{n}", expose_as_mcp_tool=True, mcp_description="Get order")
    async def order(n: int) -> dict:
        return {"n": n}

    # exclude_middleware drops Auth from the /mcp transport route (it has its own
    # auth); `if request.is_mcp` drops it from the replayed tool call. No paths.
    app.mount_mcp(transport="http", auth=_auth(), exclude_middleware=["Auth"])
    resp = app.test_client().post(
        "/mcp", json=_mcp_call_body("order", {"n": 7}), headers={"authorization": "Bearer good"}
    )
    assert resp.status_code == 200
    assert orjson.loads(orjson.loads(resp.body)["result"]["content"][0]["text"]) == {"n": 7}


def test_origin_validation():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", allowed_origins=["https://app.example.com"])
    client = app.test_client()
    body = _mcp_call_body("add", {"a": 1, "b": 1})

    # A browser Origin outside the allowlist is rejected (DNS-rebinding defense).
    bad = client.post("/mcp", json=body, headers={"origin": "https://evil.example"})
    assert bad.status_code == 403
    # An allowed Origin passes.
    ok = client.post("/mcp", json=body, headers={"origin": "https://app.example.com"})
    assert ok.status_code == 200
    # No Origin header (a non-browser client) passes.
    none = client.post("/mcp", json=body)
    assert none.status_code == 200


def test_mcpauth_requires_metadata():
    with pytest.raises(ValueError, match="resource_server_url"):
        MCPAuth(verify=_verify, authorization_servers=["https://auth.example.com"])
    with pytest.raises(ValueError, match="authorization_servers"):
        MCPAuth(verify=_verify, resource_server_url="https://api.example.com/mcp")


def test_principal_token_not_in_repr():
    p = Principal(subject="u", token="super-secret-token-value")
    assert "super-secret-token-value" not in repr(p)


def test_tool_argument_cannot_spoof_header_or_cookie():
    app = Veloce(openapi_url=None)

    @app.get(
        "/probe", expose_as_mcp_resource=False, mcp_description="Probe", expose_as_mcp_tool=True
    )
    async def probe(request: Request) -> dict:
        # A Security scheme reading a header / cookie (HTTPBearer, APIKeyHeader,
        # APIKeyCookie) must NOT see an agent-supplied value: tool arguments are
        # never seeded into the synthetic request's headers or cookies.
        return {
            "auth": request.headers.get("authorization"),
            "api_key": request.headers.get("x-api-key"),
            "cookie": request.cookies.get("session"),
        }

    out = _call(
        app,
        "probe",
        {"authorization": "Bearer spoofed", "x_api_key": "forged", "session": "hijacked"},
    )
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"auth": None, "api_key": None, "cookie": None}


def test_origin_empty_allowlist_rejects_all_browsers():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    # An explicit empty allowlist denies every browser Origin (it does not
    # silently disable the check).
    app.mount_mcp(transport="http", allowed_origins=[])
    body = _mcp_call_body("add", {"a": 1, "b": 1})
    blocked = app.test_client().post("/mcp", json=body, headers={"origin": "https://any.example"})
    assert blocked.status_code == 403
    # A non-browser client (no Origin) is still allowed.
    assert app.test_client().post("/mcp", json=body).status_code == 200


def test_get_on_mcp_endpoint_returns_405():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    # The endpoint has no standalone server-to-client stream, so a GET is the
    # spec-conformant 405 (not the app's generic method-not-allowed), with Allow.
    resp = app.test_client().get("/mcp")
    assert resp.status_code == 405
    assert resp.headers.get("allow") == "POST"


def test_unsupported_protocol_version_header_is_400():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    client = app.test_client()
    body = _mcp_call_body("add", {"a": 1, "b": 1})

    # A present, unsupported version is rejected with a JSON-RPC error at 400.
    bad = client.post("/mcp", json=body, headers={"mcp-protocol-version": "1999-01-01"})
    assert bad.status_code == 400
    assert orjson.loads(bad.body)["error"]["code"] == -32600
    # A supported version passes.
    ok = client.post("/mcp", json=body, headers={"mcp-protocol-version": "2025-06-18"})
    assert ok.status_code == 200


def test_missing_protocol_version_header_keeps_current_behavior():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    # No MCP-Protocol-Version header: the legacy path is unchanged (no 400).
    resp = app.test_client().post("/mcp", json=_mcp_call_body("add", {"a": 1, "b": 1}))
    assert resp.status_code == 200


def test_sessions_disabled_by_default():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    client = app.test_client()
    # The stateless default assigns no session id and never requires one.
    init = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert init.headers.get("mcp-session-id") is None
    call = client.post("/mcp", json=_mcp_call_body("add", {"a": 1, "b": 1}))
    assert call.status_code == 200


def test_session_id_assigned_on_initialize_and_required_after():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", sessions=True)
    client = app.test_client()

    init = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    session_id = init.headers.get("mcp-session-id")
    assert session_id
    assert orjson.loads(init.body)["result"]["serverInfo"]["name"]

    # A later request echoing the id is accepted and the id is echoed back.
    ok = client.post(
        "/mcp",
        json=_mcp_call_body("add", {"a": 2, "b": 3}),
        headers={"mcp-session-id": session_id},
    )
    assert ok.status_code == 200
    assert ok.headers.get("mcp-session-id") == session_id
    assert orjson.loads(ok.body)["result"]["content"][0]["text"] == "5"


def test_missing_session_id_is_400():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", sessions=True)
    # A non-initialize request without the session header is rejected at 400.
    resp = app.test_client().post("/mcp", json=_mcp_call_body("add", {"a": 1, "b": 1}))
    assert resp.status_code == 400
    assert orjson.loads(resp.body)["error"]["code"] == -32600


def test_unknown_session_id_is_404():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", sessions=True)
    resp = app.test_client().post(
        "/mcp",
        json=_mcp_call_body("add", {"a": 1, "b": 1}),
        headers={"mcp-session-id": "never-issued"},
    )
    assert resp.status_code == 404


def test_delete_terminates_session_then_404():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", sessions=True)
    client = app.test_client()
    init = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    session_id = init.headers["mcp-session-id"]

    # DELETE terminates the live session (HTTP 204).
    deleted = client.delete("/mcp", headers={"mcp-session-id": session_id})
    assert deleted.status_code == 204

    # A request on the terminated id is now 404; a second DELETE is too.
    after = client.post(
        "/mcp",
        json=_mcp_call_body("add", {"a": 1, "b": 1}),
        headers={"mcp-session-id": session_id},
    )
    assert after.status_code == 404
    assert client.delete("/mcp", headers={"mcp-session-id": session_id}).status_code == 404


def test_delete_without_sessions_is_405():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    # Without session management the DELETE verb is unsupported.
    resp = app.test_client().delete("/mcp")
    assert resp.status_code == 405
    assert resp.headers.get("allow") == "POST"


def test_session_id_echoed_on_sse_response():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work")
    async def work() -> str:
        return "done"

    app.mount_mcp(transport="http", sessions=True)
    client = app.test_client()
    session_id = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    ).headers["mcp-session-id"]

    resp = client.post(
        "/mcp",
        json=_mcp_call_body("work"),
        headers={"mcp-session-id": session_id, "accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("mcp-session-id") == session_id


def test_sse_stream_opens_with_priming_event_and_closes_with_retry():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work")
    async def work() -> str:
        return "done"

    app.mount_mcp(transport="http")
    resp = app.test_client().post(
        "/mcp", json=_mcp_call_body("work"), headers={"accept": "text/event-stream"}
    )
    frames = resp.body.split(b"\n\n")
    # First frame: a priming event carrying an id and an empty data field.
    assert frames[0].startswith(b"id: ")
    assert b"data: " in frames[0]
    # Last non-empty frame: a retry field hinting the reconnect delay.
    closing = [f for f in frames if f.strip()][-1]
    assert closing.strip() == b"retry: 3000"
    # The JSON-RPC response is still delivered between the two.
    events = _parse_sse(resp.body)
    assert events[-1]["result"]["content"][0]["text"] == "done"


def test_resumability_disabled_by_default_get_is_405():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work")
    async def work() -> str:
        return "done"

    app.mount_mcp(transport="http")
    client = app.test_client()
    # Without resumability a GET (even carrying Last-Event-ID) stays unsupported,
    # and a streamed POST attaches no payload event ids.
    resp = client.get("/mcp", headers={"last-event-id": "anything.0"})
    assert resp.status_code == 405
    assert resp.headers.get("allow") == "POST"
    streamed = client.post(
        "/mcp", json=_mcp_call_body("work"), headers={"accept": "text/event-stream"}
    )
    payload_frames = [f for f in streamed.body.split(b"\n\n") if b"data: {" in f]
    # The payload frame (the JSON-RPC response) carries no id field.
    assert payload_frames and all(not f.strip().startswith(b"id:") for f in payload_frames)


def test_resumable_stream_attaches_per_stream_event_ids():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work with progress")
    async def work(ctx: MCPContext) -> str:
        await ctx.report_progress(1, 2)
        await ctx.report_progress(2, 2)
        return "done"

    app.mount_mcp(transport="http", resumable=True)
    resp = app.test_client().post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "work", "arguments": {}, "_meta": {"progressToken": "p"}},
        },
        headers={"accept": "text/event-stream"},
    )
    ids = _sse_event_ids(resp.body)
    # Priming id (sequence 0) plus one id per payload event (two progress + response).
    assert len(ids) == 4
    stream_id = ids[0].rsplit(".", 1)[0]
    # Every id shares the one stream and the sequence advances 0,1,2,3.
    assert [i.rsplit(".", 1)[0] for i in ids] == [stream_id] * 4
    assert [int(i.rsplit(".", 1)[1]) for i in ids] == [0, 1, 2, 3]


def test_resume_replays_only_the_missed_tail_of_the_same_stream():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work with progress")
    async def work(ctx: MCPContext) -> str:
        await ctx.report_progress(1, 2)
        await ctx.report_progress(2, 2)
        return "done"

    app.mount_mcp(transport="http", resumable=True)
    client = app.test_client()
    first = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "work", "arguments": {}, "_meta": {"progressToken": "p"}},
        },
        headers={"accept": "text/event-stream"},
    )
    ids = _sse_event_ids(first.body)
    # Pretend the client received through the first progress event, then dropped.
    last_seen = ids[1]

    resumed = client.get("/mcp", headers={"last-event-id": last_seen})
    assert resumed.status_code == 200
    assert "text/event-stream" in resumed.content_type
    # Only the events after the acknowledged one are replayed: the second progress
    # notification and the final response.
    replayed = _parse_sse(resumed.body)
    assert len(replayed) == 2
    assert replayed[0]["method"] == "notifications/progress"
    assert replayed[0]["params"]["progress"] == 2
    assert replayed[-1]["result"]["content"][0]["text"] == "done"
    # The replayed ids stay on the originating stream.
    assert all(
        i.rsplit(".", 1)[0] == last_seen.rsplit(".", 1)[0] for i in _sse_event_ids(resumed.body)
    )


def test_resume_from_priming_id_replays_whole_stream():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work with progress")
    async def work(ctx: MCPContext) -> str:
        await ctx.report_progress(1, 1)
        return "done"

    app.mount_mcp(transport="http", resumable=True)
    client = app.test_client()
    first = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "work", "arguments": {}, "_meta": {"progressToken": "p"}},
        },
        headers={"accept": "text/event-stream"},
    )
    priming_id = _sse_event_ids(first.body)[0]
    # Resuming from the priming id (sequence 0) replays every payload event.
    resumed = client.get("/mcp", headers={"last-event-id": priming_id})
    replayed = _parse_sse(resumed.body)
    assert len(replayed) == 2
    assert replayed[-1]["result"]["content"][0]["text"] == "done"


def test_resume_does_not_cross_into_another_stream():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Echo")
    async def echo(label: str) -> str:
        return label

    app.mount_mcp(transport="http", resumable=True)
    client = app.test_client()

    def call(label: str) -> bytes:
        return client.post(
            "/mcp",
            json=_mcp_call_body("echo", {"label": label}),
            headers={"accept": "text/event-stream"},
        ).body

    first_priming = _sse_event_ids(call("a"))[0]
    call("b")  # a second, distinct stream the resume must not leak.

    # Resuming the first stream replays only its events, never the second's.
    resumed = client.get("/mcp", headers={"last-event-id": first_priming})
    replayed = _parse_sse(resumed.body)
    assert len(replayed) == 1
    assert replayed[0]["result"]["content"][0]["text"] == "a"


def test_resume_with_unknown_event_id_replays_nothing():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work")
    async def work() -> str:
        return "done"

    app.mount_mcp(transport="http", resumable=True)
    client = app.test_client()
    # An id for a stream that was never recorded yields an empty replay (still a
    # well-formed SSE response closing with the reconnect hint), not a crash.
    resumed = client.get("/mcp", headers={"last-event-id": "never-seen.3"})
    assert resumed.status_code == 200
    assert _parse_sse(resumed.body) == []
    assert b"retry: 3000" in resumed.body


def test_resume_without_last_event_id_is_405():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work")
    async def work() -> str:
        return "done"

    app.mount_mcp(transport="http", resumable=True)
    # A GET with no Last-Event-ID is not a resume; there is no standalone stream.
    resp = app.test_client().get("/mcp")
    assert resp.status_code == 405


def test_resume_get_rejects_cross_origin():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work")
    async def work() -> str:
        return "done"

    # The GET resume path must run the same DNS-rebinding defense as POST: a
    # browser Origin outside the allowlist is rejected before any replay.
    app.mount_mcp(transport="http", resumable=True, allowed_origins=["https://app.example.com"])
    blocked = app.test_client().get(
        "/mcp",
        headers={"last-event-id": "s.1", "origin": "https://evil.example"},
    )
    assert blocked.status_code == 403


def test_resume_get_rejects_unsupported_protocol_version():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work")
    async def work() -> str:
        return "done"

    app.mount_mcp(transport="http", resumable=True)
    # A present, unsupported MCP-Protocol-Version on the GET resume path is the
    # same 400 the POST path returns, not a stream.
    bad = app.test_client().get(
        "/mcp",
        headers={"last-event-id": "s.1", "mcp-protocol-version": "1999-01-01"},
    )
    assert bad.status_code == 400


def test_event_store_replays_after_eviction_window():
    # Deferred: one test needs the event store's internals, and the module
    # top deliberately imports only the public transport surface.
    from veloce.contrib.mcp.transports.event_store import (
        _MAX_EVENTS_PER_STREAM,
        SSEEventStore,
    )

    store = SSEEventStore()
    # Record more than the per-stream window so the oldest entries are evicted.
    for seq in range(1, _MAX_EVENTS_PER_STREAM + 5):
        store.record("s", seq, {"seq": seq})
    # A resume from an evicted middle id replays whatever still remains, in order,
    # rather than nothing or a crash.
    overflow = _MAX_EVENTS_PER_STREAM + 4
    missed = store.replay_after(f"s.{overflow - 2}")
    assert [p["seq"] for _, p in missed] == [overflow - 1, overflow]


def test_event_store_discards_unknown_stream_and_malformed_ids():

    store = SSEEventStore()
    store.record("s", 1, {"v": 1})
    assert store.replay_after("other.0") == []  # unknown stream
    assert store.replay_after("no-separator") == []  # malformed id
    assert store.replay_after("s.x") == []  # non-numeric sequence


async def test_sse_disconnection_does_not_cancel_the_call():

    app = Veloce(openapi_url=None)
    started = asyncio.Event()
    release = asyncio.Event()
    ran_to_completion = asyncio.Event()

    @app.mcp_tool(description="Work whose completion is observable after disconnect")
    async def work(ctx: MCPContext) -> str:
        started.set()
        # Block until the test releases the call, keeping it in flight across the
        # consumer's disconnect so cancellation (if any) would land here.
        await release.wait()
        ran_to_completion.set()
        return "done"

    stream = _stream_response(MCPServer(app), _mcp_call_body("work"), None, MCPSession())
    gen = stream._stream
    # Consume only the priming frame, then disconnect by closing the generator -
    # simulating a client dropping the SSE stream while the call is in flight.
    await gen.__anext__()
    await started.wait()
    await gen.aclose()
    # The call is still blocked (in flight) at disconnect time; release it now.
    release.set()

    # The dispatched call is left running to completion; a dropped connection is
    # not treated as the client cancelling its request (MCP transport rule).
    await asyncio.wait_for(ran_to_completion.wait(), timeout=1)
    assert ran_to_completion.is_set()


def test_mcp_context_cancelled_defaults_false():
    ctx = MCPContext("tool")
    assert ctx.cancelled is False


async def test_notifications_cancelled_cancels_in_flight_call():
    app = Veloce(openapi_url=None)
    started = asyncio.Event()
    release = asyncio.Event()
    saw_cancel = asyncio.Event()

    @app.mcp_tool(description="Cooperative work")
    async def work(ctx: MCPContext) -> str:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # The cancel notification flips the flag and unwinds the await.
            assert ctx.cancelled is True
            saw_cancel.set()
            raise
        return "done"

    server = MCPServer(app)
    call = asyncio.ensure_future(server.handle_message(_mcp_call_body("work", {})))
    await asyncio.wait_for(started.wait(), timeout=1)

    # The client cancels the in-flight request by id; the call's task unwinds.
    await server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
    )
    with pytest.raises(asyncio.CancelledError):
        await call
    await asyncio.wait_for(saw_cancel.wait(), timeout=1)
    # The cancelled request is removed from the in-flight registry once settled.
    assert server._inflight == {}


async def test_notifications_cancelled_unknown_id_is_a_no_op():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    server = MCPServer(app)
    # No request is in flight; cancelling an unknown id returns no response and
    # does not raise (the request may have already completed).
    result = await server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}}
    )
    assert result is None


async def test_notifications_cancelled_non_hashable_id_is_ignored():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    server = MCPServer(app)
    # A `requestId` that is a list/object would make the in-flight lookup key
    # unhashable; the malformed notification is ignored without raising.
    for bad_id in ([1, 2], {"k": "v"}, True):
        result = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": bad_id},
            }
        )
        assert result is None


def test_task_settle_is_idempotent_after_cancel():

    # A `tasks/cancel` racing the runner's natural completion: the cancel settles
    # the task first, and the runner's later COMPLETED settle must not overwrite
    # the recorded CANCELLED state.
    task = new_task("work", ttl_ms=1000)
    task.settle(STATUS_CANCELLED, {"content": [], "isError": True}, "cancelled")
    task.settle(STATUS_COMPLETED, {"content": [{"type": "text", "text": "done"}]})
    assert task.status == STATUS_CANCELLED
    assert task.result == {"content": [], "isError": True}


async def test_initialize_request_is_not_cancellable():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    server = MCPServer(app)
    # `initialize` is never registered as cancellable (the spec forbids cancelling
    # it), so dispatching it leaves the in-flight registry untouched.
    await server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert server._inflight == {}
    # A tracked tool call, by contrast, would register; verify the gate directly.
    assert server._track_inflight(1, "initialize") is None
    assert server._track_inflight(2, "tools/call") is not None


async def test_http_sse_call_is_cancelled_by_notification():

    app = Veloce(openapi_url=None)
    started = asyncio.Event()
    release = asyncio.Event()
    ran_to_completion = asyncio.Event()

    @app.mcp_tool(description="Work cancelled mid-flight over SSE")
    async def work(ctx: MCPContext) -> str:
        started.set()
        await release.wait()
        ran_to_completion.set()
        return "done"

    server = MCPServer(app)
    # One connection: the cancel must arrive on the same session the call runs
    # under, since cancellation is now scoped per connection.
    session = MCPSession()
    stream = _stream_response(server, _mcp_call_body("work"), None, session)
    gen = stream._stream
    await gen.__anext__()  # priming frame; schedules the runner task
    await asyncio.wait_for(started.wait(), timeout=1)

    # A cancel on the same connection reaches the concurrently-running call by id.
    await server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        session,
    )
    # Drain the stream to its close; the call never ran to completion.
    async for _ in gen:
        pass
    release.set()
    await asyncio.sleep(0)
    assert not ran_to_completion.is_set()
    assert server._inflight == {}


def test_http_log_level_does_not_bleed_across_requests():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Log then return")
    async def work(ctx: MCPContext) -> str:
        await ctx.log("info", "hello")
        return "ok"

    app.mount_mcp(transport="http")
    client = app.test_client()
    # One HTTP client raises the log floor to `error`...
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logging/setLevel",
            "params": {"level": "error"},
        },
    )
    # ...which must NOT carry into a separate request (the level is per-request,
    # not shared on the one MCPServer the HTTP transport reuses).
    resp = client.post("/mcp", json=_mcp_call_body("work"), headers={"accept": "text/event-stream"})
    messages = [e for e in _parse_sse(resp.body) if e.get("method") == "notifications/message"]
    assert len(messages) == 1  # the info log is emitted; the other request's setLevel did not bleed
