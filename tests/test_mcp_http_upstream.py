"""Tests for the http_upstream helper (contrib/mcp/http_upstream.py)."""

from __future__ import annotations

import asyncio
import json

import pytest

from veloce.contrib.mcp.errors import MCPError
from veloce.contrib.mcp.http_upstream import http_upstream

httpx2 = pytest.importorskip("httpx2")


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
