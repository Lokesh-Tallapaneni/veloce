"""The in-memory clients' receive channel — when it reports `http.disconnect`.

The channel used to answer `http.disconnect` the moment the request body ran
out. That reads as "no more input", but it is the same message a server sends
for "the peer is gone", and `request.is_disconnected()` cannot tell them apart -
so a streaming handler polling it under `TestClient` aborted on its first
iteration and emitted nothing, while the same handler served a real client
correctly.

The channel now separates the two callers by what they do: a poll peeks for one
event-loop turn and is answered nothing, while a read that is really waiting for
input is answered at once. Both answers are immediate - a reader that waits is
never parked on the response, which would have cost an in-dispatch drain (a
mounted ASGI sub-app, a middleware reading to `http.disconnect`) half a second
of real time per request.
"""

from __future__ import annotations

import asyncio
import time

from veloce import Request, StreamingResponse, Veloce
from veloce.testclient import AsyncTestClient, TestClient, _ClientChannel

# Any of these assertions would pass at a hundred times the budget on a loaded
# machine; they exist to catch a channel that waits on a timer, which the first
# version of this fix did (half a second per read).
BUDGET = 0.1


def _polling_app() -> tuple[Veloce, dict]:
    """An app whose streaming route aborts as soon as it thinks the client left."""
    app = Veloce(openapi_url=None)
    seen: dict[str, Request] = {}

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        async def gen():
            for i in range(5):
                if await request.is_disconnected():
                    yield b"ABORTED\n"
                    return
                yield f"event {i}\n".encode()

        return StreamingResponse(gen(), content_type="text/plain")

    @app.get("/plain")
    async def plain(request: Request) -> dict:
        seen["request"] = request
        return {"gone": await request.is_disconnected()}

    return app, seen


EXPECTED = "".join(f"event {i}\n" for i in range(5))


def test_a_streaming_handler_emits_its_whole_body() -> None:
    """The regression: this aborted on the first iteration and emitted nothing."""
    app, _ = _polling_app()
    with TestClient(app) as client:
        started = time.perf_counter()
        response = client.get("/events")
        elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert response.text == EXPECTED, "the handler was told its client had gone"
    # Five polls, each of which reaches the channel.
    assert elapsed < BUDGET


async def test_the_async_client_emits_its_whole_body_too() -> None:
    """Both clients share the channel, so neither may drift from the other."""
    app, _ = _polling_app()
    async with AsyncTestClient(app) as client:
        response = await client.get("/events")

    assert response.text == EXPECTED


def test_a_buffered_handler_polling_before_it_returns_is_connected() -> None:
    app, _ = _polling_app()
    with TestClient(app) as client:
        assert client.get("/plain").json() == {"gone": False}


def test_a_disconnect_is_reported_once_the_exchange_is_over() -> None:
    """The real signal survives: after the response, the client is gone."""
    app, seen = _polling_app()
    with TestClient(app) as client:
        client.get("/plain")
        request = seen["request"]
        assert client._loop.run_until_complete(request.is_disconnected()) is True


def test_a_streamed_request_body_is_not_read_as_a_disconnect() -> None:
    """The `stream=` channel answered one too, right after its terminal frame."""
    app = Veloce(openapi_url=None)

    @app.post("/echo")
    async def echo(request: Request) -> dict:
        body = await request.body()
        return {"body": body.decode(), "gone": await request.is_disconnected()}

    with TestClient(app) as client:
        assert client.post("/echo", stream=[b"aa", b"bb"]).json() == {
            "body": "aabb",
            "gone": False,
        }


async def test_a_read_past_end_of_body_is_answered_at_once() -> None:
    """A reader waiting for input that will never arrive is answered immediately.

    The property the old immediate answer was protecting, kept: an ASGI
    middleware that reads past end-of-body (introspection, fan-out, replay) is
    told the input is over rather than parked on an event nothing sets. It must
    also not be made to wait for the response - that is the half-second this
    read used to cost, mid-dispatch, on every request.
    """
    channel = _ClientChannel(b"payload", None)
    assert (await channel())["body"] == b"payload"

    started = time.perf_counter()
    message = await asyncio.wait_for(channel(), timeout=5)
    waited = time.perf_counter() - started

    assert message["type"] == "http.disconnect"
    assert waited < BUDGET, "the channel waited rather than answering"


async def test_a_finished_exchange_answers_a_read_without_yielding() -> None:
    """Once the response is complete there is not even a turn to give away."""
    channel = _ClientChannel(b"", None)
    await channel()
    channel.exchange_finished()

    started = time.perf_counter()
    assert (await channel())["type"] == "http.disconnect"
    assert time.perf_counter() - started < BUDGET


def test_a_mounted_app_that_drains_its_channel_is_not_stalled() -> None:
    """The shape that paid the timer: a sub-app reading to `http.disconnect`.

    A mounted ASGI app is handed this same channel, and draining it is a normal
    thing for one to do. No test in the suite did it, which is why a half-second
    per request got through review - it would have surfaced as a user's suite
    mysteriously crawling.
    """
    drained: list[str] = []

    async def sub_app(scope: dict, receive, send) -> None:
        while True:
            message = await receive()
            drained.append(message["type"])
            if message["type"] == "http.disconnect":
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"drained"})

    app = Veloce(openapi_url=None)
    app.mount("/sub", sub_app)

    with TestClient(app) as client:
        started = time.perf_counter()
        response = client.get("/sub/x")
        elapsed = time.perf_counter() - started

    assert response.text == "drained"
    assert "http.disconnect" in drained
    assert elapsed < BUDGET, f"draining the channel cost {elapsed:.3f}s"
