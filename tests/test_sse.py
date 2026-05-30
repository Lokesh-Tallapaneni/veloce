"""Server-Sent Events — event encoding, ASGI streaming, keep-alive heartbeat."""

from __future__ import annotations

import asyncio

import pytest

from veloce import EventSourceResponse, ServerSentEvent, Veloce

# ── ServerSentEvent.encode ───────────────────────────────────────────


def test_event_encoding():
    event = ServerSentEvent(data="hello", event="message", id="1")
    encoded = event.encode()
    assert b"id: 1" in encoded
    assert b"event: message" in encoded
    assert b"data: hello" in encoded


def test_event_multiline():
    event = ServerSentEvent(data="line1\nline2")
    encoded = event.encode()
    assert b"data: line1" in encoded
    assert b"data: line2" in encoded


def test_event_retry():
    event = ServerSentEvent(data="test", retry=5000)
    encoded = event.encode()
    assert b"retry: 5000" in encoded


# ── ASGI streaming ───────────────────────────────────────────────────


def test_eventsource_response_accepts_serversentevent_objects():
    """EventSourceResponse encodes yielded ServerSentEvent objects over ASGI."""
    app = Veloce(openapi_url=None)

    @app.get("/sse")
    async def sse(request):
        async def generate():
            yield ServerSentEvent(data="hello", event="greeting")

        return EventSourceResponse(generate())

    resp = app.test_client().get("/sse")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.content_type
    assert b"data: hello" in resp.body
    assert b"event: greeting" in resp.body


# ── Keep-alive heartbeat (ping=) ─────────────────────────────────────


async def _drain(resp: EventSourceResponse) -> list[bytes]:
    return [chunk async for chunk in resp._stream]


async def test_no_heartbeat_when_ping_none():
    """Without ping, only the real events appear — no comment frames."""

    async def gen():
        yield ServerSentEvent(data="one")
        yield ServerSentEvent(data="two")

    resp = EventSourceResponse(gen())
    chunks = await _drain(resp)
    assert all(not c.startswith(b":") for c in chunks)
    assert b"".join(chunks).count(b"data: ") == 2


async def test_heartbeat_fires_on_idle():
    """When the source idles past `ping`, a comment frame is emitted, then
    real events still flow once the source produces them."""

    async def gen():
        # Idle long enough to trip at least one ping window, then emit.
        await asyncio.sleep(0.05)
        yield ServerSentEvent(data="late")

    resp = EventSourceResponse(gen(), ping=0.01)
    chunks = await _drain(resp)
    # At least one keep-alive comment frame fired during the idle gap.
    assert any(c.startswith(b":") for c in chunks)
    # The real event still made it through after the heartbeat(s).
    assert any(b"data: late" in c for c in chunks)


async def test_heartbeat_comment_frame_format():
    """The keep-alive frame is the standard colon-prefixed comment."""

    async def gen():
        await asyncio.sleep(0.03)
        yield ServerSentEvent(data="x")

    resp = EventSourceResponse(gen(), ping=0.01)
    chunks = await _drain(resp)
    pings = [c for c in chunks if c.startswith(b":")]
    assert pings
    assert pings[0] == b": ping\r\n\r\n"


async def test_events_flow_without_idle_with_ping_set():
    """A fast source under a ping budget emits its events and terminates
    cleanly with no spurious heartbeats."""

    async def gen():
        yield ServerSentEvent(data="a")
        yield ServerSentEvent(data="b")

    resp = EventSourceResponse(gen(), ping=10)
    chunks = await _drain(resp)
    assert not any(c.startswith(b":") for c in chunks)
    assert b"".join(chunks).count(b"data: ") == 2


@pytest.mark.parametrize("bad", [0, 0.0, -1, -0.5])
def test_ping_rejects_non_positive(bad):
    """A zero or negative ping would time out instantly and flood the
    socket with heartbeat frames — reject it at construction."""

    async def gen():
        yield ServerSentEvent(data="x")

    with pytest.raises(ValueError, match="positive"):
        EventSourceResponse(gen(), ping=bad)


def test_ping_none_and_positive_are_accepted():
    async def gen():
        yield ServerSentEvent(data="x")

    EventSourceResponse(gen(), ping=None)
    EventSourceResponse(gen(), ping=0.01)
    EventSourceResponse(gen(), ping=5)
