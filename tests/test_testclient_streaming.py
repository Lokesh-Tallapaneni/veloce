"""TestClient / AsyncTestClient — streaming request bodies.

Exercises the optional `stream=` parameter on the body-carrying methods,
which feeds the ASGI app multiple `http.request` frames so the multi-chunk
`more_body` reassembly loop in `Veloce._asgi_app` is covered.
"""

from __future__ import annotations

import pytest

from veloce import RedirectResponse, Request, Veloce
from veloce.testclient import TestClient


def _echo_app() -> Veloce:
    app = Veloce()

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        return {"len": len(body), "body": body.decode("utf-8")}

    @app.post("/redir")
    async def redir():
        return RedirectResponse("/echo", status_code=307)

    return app


def test_streaming_request_body_multi_chunk():
    with TestClient(_echo_app()) as client:
        resp = client.post("/echo", stream=[b"aa", b"bb", b"cc"])
        assert resp.status_code == 200
        assert resp.json() == {"len": 6, "body": "aabbcc"}


def test_streaming_request_body_async_generator():
    async def gen():
        yield b"x"
        yield b"yz"

    with TestClient(_echo_app()) as client:
        resp = client.post("/echo", stream=gen())
        assert resp.json() == {"len": 3, "body": "xyz"}


def test_streaming_request_body_str_chunks_encoded():
    with TestClient(_echo_app()) as client:
        resp = client.post("/echo", stream=["a", "b"])
        assert resp.json() == {"len": 2, "body": "ab"}


async def test_streaming_request_body_async_client():
    app = _echo_app()
    async with app.async_test_client() as client:
        resp = await client.post("/echo", stream=[b"aa", b"bb"])
        assert resp.json() == {"len": 4, "body": "aabb"}

        async def gen():
            yield b"x"
            yield b"yz"

        resp = await client.post("/echo", stream=gen())
        assert resp.json() == {"len": 3, "body": "xyz"}


def test_streaming_body_and_explicit_body_conflict():
    with TestClient(_echo_app()) as client, pytest.raises(AssertionError):
        client.post("/echo", stream=[b"x"], json={"a": 1})


def test_streaming_body_blocks_redirect_replay():
    with (
        TestClient(_echo_app()) as client,
        pytest.raises(RuntimeError, match="cannot be replayed across redirects"),
    ):
        client.post("/redir", stream=[b"x"], follow_redirects=True)
