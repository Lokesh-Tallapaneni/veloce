"""Built-in server — the body is buffered before a non-streaming handler runs.

`request.data` and `request.get_json()` are synchronous: the body has to be in
memory by the time the handler reads them. (`request.form()` is async and was
never affected.) The ASGI path
guarantees that by draining inline before dispatch. The raw transport builds its
`Request` at headers-complete and feeds the body afterwards, so without an
equivalent step both of those accessors raised
`RuntimeError: Request body is not yet buffered` and surfaced as a 500 - on the
same handler that worked perfectly under uvicorn.

These tests pin the parity in both directions: a non-streaming route sees a
buffered body, and a `stream=True` route still gets the lazy source it consumes
itself rather than a body drained out from under it.
"""

from __future__ import annotations

import asyncio
import json

from tests._protocol import _FakeTransport
from veloce import Veloce, request
from veloce.serving.protocol import HttpProtocol
from veloce.testclient import AsyncTestClient


async def _serve(app: Veloce, *chunks: bytes) -> str:
    """Drive one request through the raw protocol; return what was written.

    Chunks are delivered as separate `data_received` calls so a test can put the
    body in a later segment than the headers - the case where the body genuinely
    is not there yet when the handler starts.
    """
    loop = asyncio.get_running_loop()
    proto = HttpProtocol(app, loop)
    proto.connection_made(_FakeTransport())
    transport = proto.transport
    assert isinstance(transport, _FakeTransport)
    for chunk in chunks:
        proto.data_received(chunk)
        # Yield between segments so the dispatch task can observe a body that is
        # still arriving rather than one already complete.
        await asyncio.sleep(0)
    if proto._server_loop is not None:
        await proto._server_loop
    return b"".join(transport.writes).decode("latin-1")


def _post(path: str, body: bytes, content_type: str = "application/json") -> bytes:
    return (
        f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.post("/json")
    async def read_json():

        return {"got": request.get_json()}

    @app.post("/data")
    async def read_data():

        return {"length": len(request.data)}

    @app.post("/form")
    async def read_form():

        form = await request.form()
        return {"name": form.get("name")}

    @app.get("/nobody")
    async def nobody():

        return {"length": len(request.data)}

    return app


# ── The sync accessors see a buffered body ───────────────────────────


async def test_get_json_is_buffered_before_the_handler_runs():
    """The original defect: this returned 500 on the raw transport."""
    written = await _serve(_app(), _post("/json", b'{"a": 1}'))
    assert "200 OK" in written
    assert '{"a":1}' in written.replace(" ", "")


async def test_data_is_buffered():
    written = await _serve(_app(), _post("/data", b"x" * 32, "application/octet-stream"))
    assert "200 OK" in written
    assert '"length":32' in written.replace(" ", "")


async def test_awaiting_form_still_works_after_the_pre_drain():
    """`form` is async, so it was never part of the defect - but the pre-drain
    must not strand it either."""
    body = b"name=ada"
    written = await _serve(_app(), _post("/form", body, "application/x-www-form-urlencoded"))
    assert "200 OK" in written
    assert "ada" in written


async def test_a_body_arriving_after_the_headers_is_still_buffered():
    """The case the fast path cannot cover: the body is genuinely not there yet."""
    body = json.dumps({"a": 2}).encode()
    head = (
        b"POST /json HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n"
    )
    written = await _serve(_app(), head, body)
    assert "200 OK" in written
    assert '{"a":2}' in written.replace(" ", "")


async def test_a_body_split_across_several_segments_is_reassembled():
    body = json.dumps({"value": "abcdefghij"}).encode()
    head = (
        b"POST /data HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n"
    )
    written = await _serve(_app(), head, body[:5], body[5:12], body[12:])
    assert "200 OK" in written
    assert f'"length":{len(body)}' in written.replace(" ", "")


async def test_a_large_body_is_buffered_whole():
    payload = json.dumps({"blob": "z" * 200_000}).encode()
    written = await _serve(_app(), _post("/data", payload, "application/json"))
    assert "200 OK" in written
    assert f'"length":{len(payload)}' in written.replace(" ", "")


# ── Edge cases around the empty-body fast path ───────────────────────


async def test_a_bodyless_get_still_answers():
    """The `at_eof` fast path: nothing to wait for, and no hang."""
    written = await _serve(_app(), b"GET /nobody HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert "200 OK" in written
    assert '"length":0' in written.replace(" ", "")


async def test_a_post_declaring_zero_length_answers():
    written = await _serve(_app(), _post("/data", b"", "application/json"))
    assert "200 OK" in written
    assert '"length":0' in written.replace(" ", "")


async def test_an_unmatched_route_still_answers_404():
    """`match` is None there, so the buffering branch must tolerate it."""
    written = await _serve(_app(), _post("/nope", b'{"a": 1}'))
    assert "404" in written


async def test_pipelined_requests_are_each_buffered():
    """Two requests in one segment: the second must not read the first's body."""
    app = _app()
    first = _post("/data", b"aaaa", "application/json").replace(b"Connection: close\r\n", b"", 1)
    second = _post("/data", b"bbbbbbb", "application/json")
    written = await _serve(app, first + second)
    stripped = written.replace(" ", "")
    assert '"length":4' in stripped
    assert '"length":7' in stripped


# ── A streaming route keeps its lazy source ──────────────────────────


async def test_a_streaming_route_is_not_pre_drained():
    """`stream=True` consumes the source itself; buffering would starve it."""
    app = Veloce(openapi_url=None)
    seen: list[int] = []

    async def echo():

        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        seen.append(total)
        return {"streamed": total}

    app.add_route("/stream", echo, methods=["POST"], stream=True)

    body = b"c" * 4096
    head = (
        b"POST /stream HTTP/1.1\r\nHost: x\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n"
    )
    written = await _serve(app, head, body[:1000], body[1000:])
    assert "200 OK" in written
    assert seen == [len(body)]
    assert '"streamed":4096' in written.replace(" ", "")


# ── Parity with the ASGI path ────────────────────────────────────────


async def test_both_transports_agree_on_the_same_handler():
    """The defect was a divergence, so the regression test is a comparison."""

    payload = {"a": 1, "b": "two"}
    async with AsyncTestClient(_app()) as client:
        asgi = (await client.post("/json", json=payload)).json()

    written = await _serve(_app(), _post("/json", json.dumps(payload).encode()))
    native = json.loads(written.split("\r\n\r\n", 1)[1])
    assert native == asgi == {"got": payload}
