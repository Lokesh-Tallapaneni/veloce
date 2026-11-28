"""The built-in server binds the path parameter an ASGI server would.

An ASGI server percent-decodes before it fills `scope["path"]`, so
`/items/a%20b` binds `"a b"`. The built-in server ascii-decoded the raw target
and stopped, so the same route on the same app bound `"a%20b"` - the handler saw
a different value depending only on how the app was served, and `%2F`, `%25`
and any non-ASCII escape came through raw.

The decode is skipped when the target carries no `%`, which is nearly every
request, so the common path pays one memchr.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce import Veloce
from veloce.serving.protocol import HttpProtocol
from veloce.testclient import TestClient


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/items/{name}")
    async def item(name: str):
        return {"name": name}

    return app


class _Transport(asyncio.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name, default=None):
        return default


def _native(target: str) -> str:
    """Drive one request through the built-in server, returning the body."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(_app(), loop)
        transport = _Transport()
        proto.connection_made(transport)
        proto.data_received(f"GET {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        # Drain the protocol's own server loop rather than sleeping a fixed
        # 50ms: what has to finish is that task, and waiting on it is both
        # exact and immediate. Every test in this module comes through here.
        server_loop = proto._server_loop
        if server_loop is not None:
            loop.run_until_complete(asyncio.wait_for(asyncio.shield(server_loop), 5))
        else:
            for _ in range(200):
                if transport.writes:
                    break
                loop.run_until_complete(asyncio.sleep(0))
        return b"".join(transport.writes).partition(b"\r\n\r\n")[2].decode("utf-8", "replace")
    finally:
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()


_TARGETS = [
    "/items/a%20b",
    "/items/plain",
    "/items/caf%C3%A9",
    "/items/a+b",
    "/items/100%25",
    "/items/a%2Bb",
]


@pytest.mark.parametrize("target", _TARGETS)
def test_both_transports_bind_the_same_value(target):
    """The defect: the same route bound a different value per transport."""
    assert _native(target) == TestClient(_app()).get(target).text, target


def test_a_percent_escape_is_decoded_on_the_built_in_server():
    assert _native("/items/a%20b") == '{"name":"a b"}'


def test_a_non_ascii_escape_is_decoded_as_utf8():
    assert _native("/items/caf%C3%A9") == '{"name":"café"}'


def test_an_escaped_percent_decodes_to_one_percent():
    assert _native("/items/100%25") == '{"name":"100%"}'


def test_a_path_with_no_escape_is_unchanged():
    """The common case takes the skip and must be byte-identical."""
    assert _native("/items/plain") == '{"name":"plain"}'


def test_a_plus_is_not_treated_as_a_space():
    """A `+` is literal in a path; only a query string reads it as a space."""
    assert _native("/items/a+b") == '{"name":"a+b"}'
