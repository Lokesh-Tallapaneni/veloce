"""`request.is_disconnected()` while a handler is streaming its response.

The flag the body source records answers only for a `stream=True` request body.
A handler streaming an SSE or long-poll response has no body source at all, so
it was told the client was still there forever and generated against a peer that
had gone. These drive the case that matters: on an ASGI server the receive
channel is polled for `http.disconnect`, and the native server records the drop
from `connection_lost`.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import make_request
from veloce import Request, StreamingResponse, Veloce
from veloce.http._body import (
    ASGIBodySource,
    BodySource,
    RequestBodySource,
    _discard_outcome,
)
from veloce.serving.protocol import HttpProtocol

from ._protocol import _FakeTransport

DISCONNECT = {"type": "http.disconnect"}


class _Channel:
    """An ASGI receive channel a test drives: one body message, then silence.

    Silence is what a real server offers once the body is in - `receive()` parks
    until the peer actually goes away - so a poll of a live channel has to come
    back empty-handed rather than take an answer it was not given.
    """

    def __init__(self, body: bytes = b"") -> None:
        self._messages: list[dict] = [{"type": "http.request", "body": body, "more_body": False}]
        self._gone = asyncio.Event()
        self.calls = 0

    def disconnect(self) -> None:
        self._gone.set()

    async def __call__(self) -> dict:
        self.calls += 1
        if self._messages:
            return self._messages.pop(0)
        await self._gone.wait()
        return DISCONNECT


async def _drive(app: Veloce, channel: _Channel, path: str = "/events") -> list[dict]:
    """Run one ASGI request against `app` and collect what it sends."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [(b"host", b"t")],
    }
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, channel, send)
    return sent


def _sse_app(ticks: list[int], limit: int) -> Veloce:
    """An app whose streaming handler polls until the client goes away."""
    app = Veloce(openapi_url=None)

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        async def gen():
            while len(ticks) < limit:
                if await request.is_disconnected():
                    return
                ticks.append(len(ticks))
                yield f"data: {len(ticks)}\n\n"
                await asyncio.sleep(0)

        return StreamingResponse(gen(), content_type="text/event-stream")

    return app


# ── the ASGI path ───────────────────────────────────────


async def test_a_streaming_handler_sees_the_client_leave() -> None:
    """The defect: this generated to the limit against a peer that had gone."""
    ticks: list[int] = []
    channel = _Channel()
    driver = asyncio.ensure_future(_drive(_sse_app(ticks, limit=500), channel))
    for _ in range(10):
        await asyncio.sleep(0)
    assert ticks, "the stream never started"

    channel.disconnect()
    await asyncio.wait_for(driver, timeout=5)

    assert len(ticks) < 500, "the handler kept generating for a client that left"


async def test_the_answer_latches_once_the_client_has_left() -> None:
    """A server may hand out its `http.disconnect` once; the answer must not."""
    channel = _Channel()
    request = make_request(receive=channel)
    assert await request.is_disconnected() is False

    channel.disconnect()
    assert await request.is_disconnected() is True
    # The message has been taken off the channel now, so an unlatched re-poll
    # would park on the next one and answer False.
    for _ in range(3):
        assert await request.is_disconnected() is True


async def test_a_connected_client_is_never_reported_as_gone() -> None:
    ticks: list[int] = []
    sent = await _drive(_sse_app(ticks, limit=5), _Channel())

    assert len(ticks) == 5, "the stream stopped early for a client that was there"
    body = b"".join(m["body"] for m in sent if m["type"] == "http.response.body")
    assert body == b"".join(f"data: {i + 1}\n\n".encode() for i in range(5))


async def test_every_poll_of_a_live_channel_answers_false() -> None:
    channel = _Channel()
    request = make_request(receive=channel)
    for _ in range(5):
        assert await request.is_disconnected() is False
    assert channel.calls == 5, "the poll never reached the channel"


async def test_a_plain_buffered_request_is_unaffected() -> None:
    """A non-streaming route answers False and serves its body as before."""
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index(request: Request) -> dict:
        return {"gone": await request.is_disconnected()}

    sent = await _drive(app, _Channel(), path="/")
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    body = b"".join(m["body"] for m in sent if m["type"] == "http.response.body")
    assert body == b'{"gone":false}'


async def test_a_mounted_route_is_answered_too() -> None:
    """The sub-app is dispatched with a fresh request; it carries the channel."""
    sub = Veloce(openapi_url=None)

    @sub.get("/ping")
    async def ping(request: Request) -> dict:
        return {"gone": await request.is_disconnected()}

    app = Veloce(openapi_url=None)
    app.mount("/sub", sub)

    live = await _drive(app, _Channel(), path="/sub/ping")
    assert (
        b"".join(m["body"] for m in live if m["type"] == "http.response.body") == b'{"gone":false}'
    )

    gone = _Channel()
    gone.disconnect()
    sent = await _drive(app, gone, path="/sub/ping")
    assert (
        b"".join(m["body"] for m in sent if m["type"] == "http.response.body") == b'{"gone":true}'
    )


async def test_a_request_with_no_channel_answers_false() -> None:
    assert await make_request().is_disconnected() is False


async def test_an_abandoned_poll_leaves_the_channel_usable() -> None:
    """The cancelled `receive()` must not swallow the message the next one wants."""
    channel = _Channel()
    source = ASGIBodySource(channel, body_complete=True)
    assert await source.poll_disconnected() is False
    channel.disconnect()
    assert await source.poll_disconnected() is True


async def test_a_channel_that_cannot_answer_reports_the_client_present() -> None:
    """No answer is not an answer - a refusing channel must not abort a stream."""

    async def receive() -> dict:
        raise RuntimeError("receive refused")

    source = ASGIBodySource(receive, body_complete=True)
    assert await source.poll_disconnected() is False
    assert source.disconnected is False


# ── the request-body path, unchanged ────────────────────


async def test_a_stream_true_body_still_reports_its_own_disconnect() -> None:
    messages = iter([{"type": "http.request", "body": b"a", "more_body": True}, DISCONNECT])

    async def receive() -> dict:
        return next(messages)

    source = ASGIBodySource(receive)
    request = make_request(body_source=source)
    assert await request.is_disconnected() is False
    assert await source.read() == b"a"
    assert await request.is_disconnected() is True


async def test_a_body_still_arriving_is_not_polled() -> None:
    """The consumer owns the channel; a poll would take the chunk it waits for."""
    channel = _Channel(b"chunk")
    source = ASGIBodySource(channel)
    request = make_request(body_source=source)
    assert await request.is_disconnected() is False
    assert channel.calls == 0, "the poll consumed a message the handler needed"
    assert await source.read() == b"chunk"


def test_both_sources_declare_the_poll() -> None:
    for cls in (RequestBodySource, ASGIBodySource):
        assert cls.poll_disconnected is not BodySource.poll_disconnected


def test_the_contract_asks_for_the_poll_and_not_the_plain_read() -> None:
    """One concept, one required member: the one a consumer actually calls."""
    with pytest.raises(TypeError, match="poll_disconnected"):

        class NoPoll(BodySource):
            __slots__ = ()

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def read(self):
                return b""

            @property
            def disconnected(self):
                return False

    class NoPlainRead(BodySource):
        __slots__ = ()

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def read(self):
            return b""

        async def poll_disconnected(self):
            return False

    # Defining it at all is the assertion: omitting the plain read is allowed.
    assert issubclass(NoPlainRead, BodySource)


@pytest.mark.parametrize("how", ["feed_eof", "mark_disconnected"])
def test_a_native_source_records_the_drop_either_way(how: str) -> None:
    """Mid-body and mid-response arrive at one flag with one meaning."""
    source = RequestBodySource()
    source.feed_eof()
    assert source.disconnected is False
    if how == "feed_eof":
        source.feed_eof(disconnected=True)
    else:
        source.mark_disconnected()
    assert source.disconnected is True
    assert source.at_eof is True


# ── the native path ─────────────────────────────────────


async def _native_sse_protocol() -> tuple[HttpProtocol, dict, asyncio.Event]:
    """A native connection serving an SSE route that parks mid-response."""
    app = Veloce(openapi_url=None)
    seen: dict[str, object] = {}
    held = asyncio.Event()

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        seen["request"] = request

        async def gen():
            yield "data: 1\n\n"
            await held.wait()
            yield "data: 2\n\n"

        return StreamingResponse(gen(), content_type="text/event-stream")

    protocol = HttpProtocol(app, asyncio.get_running_loop())
    protocol.connection_made(_FakeTransport())
    protocol.data_received(b"GET /events HTTP/1.1\r\nHost: t\r\n\r\n")
    for _ in range(12):
        await asyncio.sleep(0)
    return protocol, seen, held


async def test_the_native_server_reports_a_client_that_leaves_mid_response() -> None:
    protocol, seen, held = await _native_sse_protocol()
    request = seen.get("request")
    assert isinstance(request, Request), "the streaming handler never started"
    assert await request.is_disconnected() is False, "the client is still there"

    protocol.connection_lost(None)
    for _ in range(6):
        await asyncio.sleep(0)

    assert await request.is_disconnected() is True, (
        "the client went away mid-response and the request still reports connected"
    )
    held.set()


async def test_the_native_server_does_not_report_a_live_client() -> None:
    protocol, seen, held = await _native_sse_protocol()
    request = seen.get("request")
    assert isinstance(request, Request)
    held.set()
    for _ in range(12):
        await asyncio.sleep(0)

    assert await request.is_disconnected() is False
    protocol.connection_lost(None)


# ── the probe leaves nothing behind ─────────────────────────────────


async def test_an_abandoned_probe_task_has_its_outcome_read():
    """`_discard_outcome` is what keeps a cancelled probe out of the logs."""

    async def boom() -> None:
        raise RuntimeError("probe")

    task = asyncio.ensure_future(boom())
    await asyncio.sleep(0)
    _discard_outcome(task)
    assert task.done()
    # Already retrieved by the callback, so asking again is free rather than
    # the first read - which is the whole point on a per-event polling path.
    assert isinstance(task.exception(), RuntimeError)


async def test_a_cancelled_probe_task_is_tolerated():
    """The negative: a cancelled task has no exception to read."""

    async def forever() -> None:
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(forever())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _discard_outcome(task)  # must not raise


async def test_polling_a_disconnect_does_not_accumulate_tasks():
    """A streaming handler polls this per event; each poll abandons a task."""

    class _Idle:
        async def __call__(self) -> dict:
            await asyncio.sleep(3600)
            return {}

    source = ASGIBodySource(_Idle(), body_complete=True)
    request = make_request(body_source=source)

    assert await request.is_disconnected() is False
    settled = len(asyncio.all_tasks())
    for _ in range(5):
        assert await request.is_disconnected() is False
    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) <= settled


async def test_an_idle_keep_alive_connection_does_not_retain_the_last_body_source():
    """`_serving_source` exists so `connection_lost` can mark an in-flight
    request disconnected. Left set after dispatch, it keeps the finished
    request's source - and anything still queued in it - reachable for as long
    as the connection stays open."""
    app = Veloce()

    @app.post("/echo")
    async def echo(request: Request) -> dict:
        return {"n": len(await request.body())}

    protocol = HttpProtocol(app, asyncio.get_running_loop())
    protocol.connection_made(_FakeTransport())
    protocol.data_received(b"POST /echo HTTP/1.1\r\nHost: t\r\nContent-Length: 4\r\n\r\nabcd")
    for _ in range(12):
        await asyncio.sleep(0)

    assert protocol._serving_source is None


async def test_a_raising_dispatch_still_releases_the_body_source(monkeypatch):
    """The release sits in `finally`.

    A handler that raises does NOT reach this: the dispatcher turns that into a
    500 and returns normally. What does is `_dispatch` itself failing, so that
    is what is forced here - otherwise the test passes with or without the
    guard, which is how the first version of it was written.
    """
    app = Veloce()

    @app.post("/boom")
    async def boom(request: Request) -> dict:
        return {"ok": True}

    protocol = HttpProtocol(app, asyncio.get_running_loop())
    protocol.connection_made(_FakeTransport())

    async def exploding_dispatch(*args, **kwargs):
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(type(protocol), "_dispatch", exploding_dispatch)
    protocol.data_received(b"POST /boom HTTP/1.1\r\nHost: t\r\nContent-Length: 4\r\n\r\nabcd")
    for _ in range(12):
        await asyncio.sleep(0)

    assert protocol._serving_source is None
