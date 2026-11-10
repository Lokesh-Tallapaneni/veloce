"""Tests for the http_upstream helper (contrib/mcp/http_upstream.py)."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from veloce.contrib.mcp.errors import MCPError
from veloce.contrib.mcp.http_upstream import http_upstream

httpx2 = pytest.importorskip("httpx2")


def test_import_error_on_a_pre_311_interpreter_names_the_python_floor(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx2", None)
    monkeypatch.setattr(sys, "version_info", (3, 10, 0, "final", 0))

    with pytest.raises(RuntimeError, match="Python >= 3.11"):
        http_upstream("http://upstream/mcp")


def test_import_error_on_a_311_plus_interpreter_names_the_pip_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx2", None)
    monkeypatch.setattr(sys, "version_info", (3, 11, 0, "final", 0))

    with pytest.raises(RuntimeError, match=r"pip install veloceframework\[mcp-proxy\]") as excinfo:
        http_upstream("http://upstream/mcp")
    assert "Python >= 3.11" not in str(excinfo.value)


def _envelope(payload: dict, *, result: dict | None = None, error: dict | None = None) -> dict:
    envelope: dict = {"jsonrpc": "2.0", "id": payload.get("id")}
    if error is not None:
        envelope["error"] = error
    else:
        envelope["result"] = result
    return envelope


def _json_response(payload: dict, result: dict, *, session_id: str | None = "sess-1"):
    headers = {"content-type": "application/json"}
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return httpx2.Response(200, json=_envelope(payload, result=result), headers=headers)


def _error_response(payload: dict, code: int, message: str, *, session_id: str | None = "sess-1"):
    headers = {"content-type": "application/json"}
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return httpx2.Response(
        200, json=_envelope(payload, error={"code": code, "message": message}), headers=headers
    )


async def test_first_call_performs_handshake_before_the_real_request():
    calls = []

    async def handler(request):
        payload = json.loads(request.read())
        calls.append(payload["method"])
        if payload["method"] == "notifications/initialized":
            return httpx2.Response(202)
        if payload["method"] == "initialize":
            return _json_response(payload, {"protocolVersion": "2025-11-25"})
        return _json_response(payload, {"tools": []})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream("http://upstream/mcp", client=client)

    result = await request("tools/list", {})

    assert result == {"tools": []}
    assert calls == ["initialize", "notifications/initialized", "tools/list"]


async def test_a_response_with_no_session_header_does_not_blank_the_held_one():
    seen = []

    async def handler(request):
        payload = json.loads(request.read())
        seen.append(request.headers.get("mcp-session-id"))
        if payload["method"] == "notifications/initialized":
            return httpx2.Response(202)
        if payload["method"] == "initialize":
            return _json_response(payload, {"protocolVersion": "2025-11-25"}, session_id="sess-1")
        return _json_response(payload, {"tools": []}, session_id=None)  # upstream doesn't repeat it

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream("http://upstream/mcp", client=client)

    await request("tools/list", {})
    await request("tools/list", {})

    assert seen == [None, "sess-1", "sess-1", "sess-1"]


async def test_upstream_error_envelope_raises_mcperror_with_the_upstreams_own_code():
    async def handler(request):
        payload = json.loads(request.read())
        if payload["method"] == "notifications/initialized":
            return httpx2.Response(202)
        if payload["method"] == "initialize":
            return _json_response(payload, {"protocolVersion": "2025-11-25"})
        return _error_response(payload, -32601, "no such tool")

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream("http://upstream/mcp", client=client)

    with pytest.raises(MCPError) as excinfo:
        await request("tools/call", {"name": "missing"})
    assert excinfo.value.code == -32601


async def test_in_band_tool_error_is_returned_not_raised():
    async def handler(request):
        payload = json.loads(request.read())
        if payload["method"] == "notifications/initialized":
            return httpx2.Response(202)
        if payload["method"] == "initialize":
            return _json_response(payload, {"protocolVersion": "2025-11-25"})
        return _json_response(
            payload, {"isError": True, "content": [{"type": "text", "text": "boom"}]}
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream("http://upstream/mcp", client=client)

    result = await request("tools/call", {"name": "x"})

    assert result["isError"] is True


async def test_modern_era_upstream_fails_fast_at_handshake():
    async def handler(request):
        payload = json.loads(request.read())
        return _json_response(payload, {"protocolVersion": "2026-07-28"})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream("http://upstream/mcp", client=client)

    with pytest.raises(RuntimeError, match="modern"):
        await request("tools/list", {})


async def test_two_concurrent_first_calls_perform_one_handshake():
    state = {"init_count": 0}

    async def handler(request):
        payload = json.loads(request.read())
        if payload["method"] == "initialize":
            state["init_count"] += 1
            await asyncio.sleep(0.01)  # force the second call to contend for the lock
            return _json_response(payload, {"protocolVersion": "2025-11-25"})
        if payload["method"] == "notifications/initialized":
            return httpx2.Response(202)
        return _json_response(payload, {"tools": []})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream("http://upstream/mcp", client=client)

    results = await asyncio.gather(request("tools/list", {}), request("tools/list", {}))

    assert results == [{"tools": []}, {"tools": []}]
    assert state["init_count"] == 1


async def test_sse_response_is_read_until_the_result_event():
    sse_body = (
        b'event: message\ndata: {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}\n\n'
        b'event: message\ndata: {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}\n\n'
    )

    async def handler(request):
        payload = json.loads(request.read())
        if payload["method"] == "notifications/initialized":
            return httpx2.Response(202)
        if payload["method"] == "initialize":
            return _json_response(payload, {"protocolVersion": "2025-11-25"})
        return httpx2.Response(
            200,
            content=sse_body,
            headers={"content-type": "text/event-stream", "mcp-session-id": "sess-1"},
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream("http://upstream/mcp", client=client)

    result = await request("tools/list", {})

    assert result == {"tools": []}


async def test_sse_response_skips_an_empty_priming_event_before_the_result():
    # A real Veloce MCP server opens its SSE stream with `id: 0\ndata: \n\n`
    # (transports/http.py's "priming event") before any JSON-RPC content.
    sse_body = (
        b"id: 0\ndata: \n\n"
        b'event: message\ndata: {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}\n\n'
    )

    async def handler(request):
        payload = json.loads(request.read())
        if payload["method"] == "notifications/initialized":
            return httpx2.Response(202)
        if payload["method"] == "initialize":
            return _json_response(payload, {"protocolVersion": "2025-11-25"})
        return httpx2.Response(
            200,
            content=sse_body,
            headers={"content-type": "text/event-stream", "mcp-session-id": "sess-1"},
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream("http://upstream/mcp", client=client)

    result = await request("tools/list", {})

    assert result == {"tools": []}


async def test_session_expiry_triggers_one_reinit_and_retry():
    calls = []
    state = {"init_count": 0}

    async def handler(request):
        payload = json.loads(request.read())
        calls.append(payload["method"])
        if payload["method"] == "initialize":
            state["init_count"] += 1
            return _json_response(
                payload, {"protocolVersion": "2025-11-25"}, session_id=f"sess-{state['init_count']}"
            )
        if payload["method"] == "notifications/initialized":
            return httpx2.Response(202)
        if request.headers.get("mcp-session-id") == "sess-1":
            return httpx2.Response(404)
        return _json_response(payload, {"tools": []}, session_id=None)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream("http://upstream/mcp", client=client)

    result = await request("tools/list", {})

    assert result == {"tools": []}
    assert calls == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert state["init_count"] == 2


async def test_a_second_404_after_reinit_propagates_without_looping():
    async def handler(request):
        payload = json.loads(request.read())
        if payload["method"] == "initialize":
            return _json_response(payload, {"protocolVersion": "2025-11-25"})
        if payload["method"] == "notifications/initialized":
            return httpx2.Response(202)
        return httpx2.Response(404)  # every tools/list 404s, even after reinit

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream("http://upstream/mcp", client=client)

    with pytest.raises(httpx2.HTTPStatusError):
        await asyncio.wait_for(request("tools/list", {}), timeout=2)


async def test_concurrent_session_expiry_triggers_one_reinit():
    state = {"init_count": 0, "stray_sessionless_calls": 0}

    async def handler(request):
        payload = json.loads(request.read())
        if payload["method"] == "initialize":
            state["init_count"] += 1
            if state["init_count"] == 2:
                # Sleeps while the retry-path lock is held, so the second
                # concurrent caller's own 404 (below) has to contend for
                # that same lock instead of finding the session already
                # fixed - without the lock it would race in here too.
                await asyncio.sleep(0.01)
            return _json_response(
                payload, {"protocolVersion": "2025-11-25"}, session_id=f"sess-{state['init_count']}"
            )
        if payload["method"] == "notifications/initialized":
            return httpx2.Response(202)
        if state["init_count"] < 2:
            # Without this, MockTransport never yields between an ordinary
            # request and its response, so the first concurrent caller runs
            # straight through its entire retry cycle before the second one
            # ever gets scheduled.
            await asyncio.sleep(0.01)
            return httpx2.Response(404)
        if not request.headers.get("mcp-session-id"):
            # A lock-less retry could reach here before the re-handshake
            # that was already in flight assigns a new session.
            state["stray_sessionless_calls"] += 1
            return httpx2.Response(400)
        return _json_response(payload, {"tools": []}, session_id=None)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream("http://upstream/mcp", client=client)

    results = await asyncio.gather(request("tools/list", {}), request("tools/list", {}))

    assert results == [{"tools": []}, {"tools": []}]
    assert state["init_count"] == 2  # one initial handshake + exactly one re-handshake, not three
    assert state["stray_sessionless_calls"] == 0


async def test_a_capitalized_caller_header_does_not_leak_past_the_protocol_override():
    async def handler(request):
        payload = json.loads(request.read())
        if payload["method"] == "initialize":
            return _json_response(payload, {"protocolVersion": "2025-11-25"})
        if payload["method"] == "notifications/initialized":
            return httpx2.Response(202)
        # "Accept" (capitalized) collides with the protocol header's lowercase
        # "accept" at the HTTP level even though they're different dict keys.
        assert request.headers.get("accept") == "application/json, text/event-stream"
        return _json_response(payload, {"tools": []})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream(
        "http://upstream/mcp",
        client=client,
        headers={"Accept": "text/plain"},
    )

    await request("tools/list", {})


async def test_supplied_client_is_untouched_by_aclose():
    async def handler(request):
        return httpx2.Response(500)  # never actually called

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream("http://upstream/mcp", client=client)

    await request.aclose()

    assert not client.is_closed


async def test_omitted_client_is_closed_and_session_deleted_by_aclose(monkeypatch):
    deletes = []

    async def handler(request):
        if request.method == "DELETE":
            deletes.append(request.headers.get("mcp-session-id"))
            return httpx2.Response(204)
        payload = json.loads(request.read())
        if payload["method"] == "initialize":
            return _json_response(payload, {"protocolVersion": "2025-11-25"})
        if payload["method"] == "notifications/initialized":
            return httpx2.Response(202)
        return _json_response(payload, {"tools": []})

    real_async_client = httpx2.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx2.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", fake_async_client)

    request = http_upstream("http://upstream/mcp")  # no client= -> owns its own
    await request("tools/list", {})
    owned_client = request._client

    await request.aclose()

    assert deletes == ["sess-1"]
    assert owned_client.is_closed


async def test_headers_reach_every_request_and_protocol_headers_win_on_collision():
    seen = []

    async def handler(request):
        seen.append(dict(request.headers))
        payload = json.loads(request.read())
        if payload["method"] == "initialize":
            return _json_response(payload, {"protocolVersion": "2025-11-25"})
        if payload["method"] == "notifications/initialized":
            return httpx2.Response(202)
        return _json_response(payload, {"tools": []})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    request = http_upstream(
        "http://upstream/mcp",
        client=client,
        headers={"authorization": "Bearer tok", "accept": "text/plain"},  # "accept" collides
    )

    await request("tools/list", {})

    assert all(h["authorization"] == "Bearer tok" for h in seen)
    assert all(h["accept"] == "application/json, text/event-stream" for h in seen)
    # Every request past the handshake carries the negotiated version - the
    # design's own required header (§5.2 step 2), never actually asserted
    # until this test.
    assert seen[-1]["mcp-protocol-version"] == "2025-11-25"
