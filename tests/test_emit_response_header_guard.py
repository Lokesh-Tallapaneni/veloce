"""The three ASGI emit paths apply the same content-type splitting guard.

The ASGI emit path bypasses `Response.encode()`, so the response-splitting guard
that `encode()` would have applied has to be applied at the emit site instead.
`_asgi_app`'s buffered branch and its streaming branch both do that before
encoding `response.content_type`; `AsgiMixin._emit_response` called `.encode()`
bare, so a `content_type` carrying CR or LF left it as a raw ASGI header value:

    (b"content-type", b"text/plain\\r\\nX-Injected: yes")

The response's own headers were never exposed - all three paths build those
through `_build_asgi_headers`, which guards them - so this was the framework
default alone. It is still the general-purpose "emit an already-built Response"
entry point, reached by the 413 and by `_emit_error`, and its own docstring
states the no-divergence invariant in prose.

`test_emit_response_bodiless.py` holds the same invariant for the bodiless-status
rule, which is a previous round of these copies drifting apart. Prose did not
keep them in step either time, so each rule gets a test.
"""

from __future__ import annotations

import pytest

from veloce import Response, Veloce
from veloce.http.response import StreamingResponse
from veloce.testclient import TestClient

#: A content-type that would split the header block if emitted unguarded.
_SPLITTING_CONTENT_TYPE = "text/plain\r\nX-Injected: yes"


async def test_the_cold_emit_path_refuses_a_splitting_content_type():
    """The divergence: this path encoded it bare and shipped the split value."""
    app = Veloce(openapi_url=None)
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    with pytest.raises(ValueError, match="control character"):
        await app._emit_response(send, Response(body=b"x", content_type=_SPLITTING_CONTENT_TYPE))
    assert not any(b"X-Injected" in value for _, value in _headers_of(sent))


def _headers_of(sent: list[dict]) -> list[tuple[bytes, bytes]]:
    """The header list of the response-start message, or nothing if none was sent."""
    for message in sent:
        if message.get("type") == "http.response.start":
            return list(message["headers"])
    return []


@pytest.mark.parametrize("path", ["/buffered", "/streamed"])
def test_the_hot_emit_paths_refuse_a_splitting_content_type(path):
    """The two branches that were already guarded, so the parity is asserted."""
    app = Veloce(openapi_url=None)

    @app.get("/buffered")
    async def buffered() -> Response:
        return Response(body=b"x", content_type=_SPLITTING_CONTENT_TYPE)

    @app.get("/streamed")
    async def streamed() -> StreamingResponse:
        async def gen():
            yield b"a"

        return StreamingResponse(gen(), content_type=_SPLITTING_CONTENT_TYPE)

    with pytest.raises(ValueError, match="control character"):
        TestClient(app).get(path)


async def test_an_ordinary_content_type_still_reaches_the_cold_path():
    """The guard must refuse the poison without refusing everything else."""
    app = Veloce(openapi_url=None)
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await app._emit_response(send, Response(body=b"x", content_type="text/plain"))
    assert (b"content-type", b"text/plain") in _headers_of(sent)
