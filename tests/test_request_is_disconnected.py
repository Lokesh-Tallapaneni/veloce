"""Request.is_disconnected() — buffered routes and streaming routes.

A non-streaming route has its body drained before the handler runs, so the
answer is always False there. The method used to hardcode that for every route,
which stopped being true when `stream=True` shipped: a streaming handler runs
while the body is still arriving, so the client really can go away mid-handler
and the method reported otherwise.
"""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Request
from veloce.http._body import ASGIBodySource


def _req() -> Request:
    return make_request(method="GET", path="/", query_string="", headers={}, body=b"")


async def test_is_disconnected_returns_false():
    # Body is fully buffered before dispatch — never disconnected.
    assert await _req().is_disconnected() is False


async def test_is_disconnected_is_awaitable():
    coro = _req().is_disconnected()
    result = await coro
    assert result is False


async def test_is_disconnected_usable_in_handler_poll_pattern():
    """the ASGI convention handlers poll it in a loop — must terminate immediately."""
    req = _req()
    polls = 0
    while not await req.is_disconnected() and polls < 3:
        polls += 1
    assert polls == 3


# ── A streaming route reports a real disconnect ──────────────────────


def _streaming_req(messages: list[dict]) -> tuple[ASGIBodySource, Request]:
    stream = iter(messages)

    async def receive() -> dict:
        return next(stream)

    source = ASGIBodySource(receive)
    request = Request(
        method="POST", path="/", query_string="", headers={}, body=b"", body_source=source
    )
    return source, request


async def test_a_streaming_request_reports_a_disconnect():
    """The client vanished mid-upload and the method still answered False."""
    source, request = _streaming_req(
        [
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    async for _ in source:
        pass
    assert await request.is_disconnected() is True


async def test_a_streaming_request_is_connected_before_the_disconnect_arrives():
    source, request = _streaming_req(
        [
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    assert await request.is_disconnected() is False


async def test_a_cleanly_completed_stream_is_not_a_disconnect():
    """`_done` covers both endings; only one of them is a disconnect."""
    source, request = _streaming_req([{"type": "http.request", "body": b"ab", "more_body": False}])
    async for _ in source:
        pass
    assert await request.is_disconnected() is False


async def test_a_disconnect_seen_by_read_is_reported_too():
    """`read()` and `__anext__` are separate consumers of the same messages."""
    source, request = _streaming_req(
        [
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    await source.read()
    assert await request.is_disconnected() is True
