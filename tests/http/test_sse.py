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


# ── Comment field ────────────────────────────────────────────────────


def test_comment_field_emits_colon_line():
    encoded = ServerSentEvent(data="x", comment="hello").encode()
    assert b": hello" in encoded
    assert b"data: x" in encoded
    # The comment line precedes the data line.
    assert encoded.index(b": hello") < encoded.index(b"data: x")


def test_comment_only_event():
    assert ServerSentEvent(comment="connected").encode() == b": connected\n\n"


def test_multiline_comment_split():
    encoded = ServerSentEvent(data="x", comment="a\nb").encode()
    assert b": a" in encoded
    assert b": b" in encoded


def test_comment_crlf_normalized():
    encoded = ServerSentEvent(comment="a\r\nb").encode()
    # CRLF is normalised to two separate `: ` lines, not one mangled line.
    assert encoded == b": a\n: b\n\n"


# ── Field validation (WHATWG SSE) ────────────────────────────────────


@pytest.mark.parametrize("bad_id", ["a\nb", "a\rb", "a\x00b"])
def test_invalid_id_rejected(bad_id):
    with pytest.raises(ValueError):
        ServerSentEvent(data="x", id=bad_id)


@pytest.mark.parametrize("bad_event", ["a\nb", "a\rb"])
def test_newline_in_event_rejected(bad_event):
    with pytest.raises(ValueError):
        ServerSentEvent(data="x", event=bad_event)


def test_valid_id_and_event_encode():
    encoded = ServerSentEvent(data="x", event="msg", id="42").encode()
    assert b"id: 42" in encoded and b"event: msg" in encoded


def test_multiline_data_still_permitted():
    encoded = ServerSentEvent(data="l1\nl2").encode()
    assert b"data: l1" in encoded and b"data: l2" in encoded


def test_json_with_nul_id_raises():
    with pytest.raises(ValueError):
        ServerSentEvent.json({"a": 1}, id="x\x00y")


def test_int_id_is_coerced_not_rejected():
    # An int id (off-contract but historically accepted) is coerced to str,
    # not crashed on the membership-test gate.
    assert b"id: 7" in ServerSentEvent(data="x", id=7).encode()


# ── ServerSentEvent.json ─────────────────────────────────────────────


def test_json_serializes_dict_payload():
    event = ServerSentEvent.json({"a": 1, "b": "x"})
    assert event.data == '{"a":1,"b":"x"}'
    assert b'data: {"a":1,"b":"x"}' in event.encode()


def test_json_serializes_list_payload():
    event = ServerSentEvent.json([1, 2, 3])
    assert event.data == "[1,2,3]"
    assert b"data: [1,2,3]" in event.encode()


def test_json_quotes_string_payload():
    """A bare string is JSON-quoted, unlike the raw data= constructor."""
    event = ServerSentEvent.json("hello")
    assert event.data == '"hello"'
    assert b'data: "hello"' in event.encode()


def test_json_forwards_metadata_fields():
    event = ServerSentEvent.json({"n": 1}, event="update", id="7", retry=3000)
    encoded = event.encode()
    assert b"event: update" in encoded
    assert b"id: 7" in encoded
    assert b"retry: 3000" in encoded


def test_json_raw_constructor_stays_unescaped():
    """The plain constructor is the raw escape hatch - no JSON quoting."""
    assert ServerSentEvent(data="hello").data == "hello"


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


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_ping_rejects_non_finite(bad):
    """NaN slips past a bare `<= 0` check (it fails every comparison) and
    Infinity passes `> 0` yet is meaningless as an `asyncio.wait` timeout —
    both must be rejected at construction."""

    async def gen():
        yield ServerSentEvent(data="x")

    with pytest.raises(ValueError, match="finite positive"):
        EventSourceResponse(gen(), ping=bad)


def test_ping_none_and_positive_are_accepted():
    async def gen():
        yield ServerSentEvent(data="x")

    EventSourceResponse(gen(), ping=None)
    EventSourceResponse(gen(), ping=0.01)
    EventSourceResponse(gen(), ping=5)


# ── Configurable ping comment ────────────────────────────────────────


def test_default_ping_frame_unchanged():
    """The default keep-alive frame stays byte-for-byte `: ping\\r\\n\\r\\n`."""

    async def gen():
        yield ServerSentEvent(data="x")

    resp = EventSourceResponse(gen(), ping=0.01)
    assert resp._ping_frame == b": ping\r\n\r\n"


async def test_configurable_ping_comment():
    """A custom ping_comment is emitted as the keep-alive frame on idle."""

    async def gen():
        await asyncio.sleep(0.05)
        yield ServerSentEvent(data="late")

    resp = EventSourceResponse(gen(), ping=0.01, ping_comment="keepalive")
    chunks = await _drain(resp)
    pings = [c for c in chunks if c.startswith(b":")]
    assert pings
    assert pings[0] == b": keepalive\r\n\r\n"


def test_ping_comment_without_ping_raises():
    async def gen():
        yield ServerSentEvent(data="x")

    with pytest.raises(ValueError, match="ping"):
        EventSourceResponse(gen(), ping_comment="x")


def test_ping_comment_multiline_rejected():
    async def gen():
        yield ServerSentEvent(data="x")

    with pytest.raises(ValueError, match="newline"):
        EventSourceResponse(gen(), ping=1, ping_comment="a\nb")


# ── Bare-value coercion ──────────────────────────────────────────────


async def test_yield_dict_coerced_to_json_data():
    """A yielded Mapping becomes a single `data:` field carrying its JSON."""

    async def gen():
        yield {"x": 1, "y": "z"}

    chunks = await _drain(EventSourceResponse(gen()))
    body = b"".join(chunks)
    assert body == b'data: {"x":1,"y":"z"}\n\n'


async def test_yield_int_and_float_coerced_to_text_data():
    async def gen():
        yield 42
        yield 3.5

    chunks = await _drain(EventSourceResponse(gen()))
    assert chunks == [b"data: 42\n\n", b"data: 3.5\n\n"]


async def test_yield_bool_coerced_to_text_data():
    async def gen():
        yield True

    chunks = await _drain(EventSourceResponse(gen()))
    assert chunks == [b"data: True\n\n"]


async def test_coercion_under_ping_window():
    """Coercion also applies on the heartbeat-bounded encode path."""

    async def gen():
        yield {"a": 1}
        yield 7

    chunks = await _drain(EventSourceResponse(gen(), ping=1))
    body = b"".join(c for c in chunks if not c.startswith(b":"))
    assert b'data: {"a":1}\n\n' in body
    assert b"data: 7\n\n" in body


async def test_str_and_bytes_fast_paths_unchanged():
    """str/bytes keep the raw passthrough (no `data:` wrapping)."""

    async def gen():
        yield "raw-line\n\n"
        yield b"raw-bytes"

    chunks = await _drain(EventSourceResponse(gen()))
    assert chunks == [b"raw-line\n\n", b"raw-bytes"]


async def test_every_yielded_chunk_is_bytes():
    """Coerced values never leak a non-bytes object into the chunk writer."""

    async def gen():
        yield {"k": "v"}
        yield 1
        yield 2.0
        yield "s"
        yield b"b"

    chunks = await _drain(EventSourceResponse(gen()))
    assert all(isinstance(c, bytes) for c in chunks)
