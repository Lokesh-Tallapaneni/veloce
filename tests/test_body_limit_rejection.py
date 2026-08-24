"""One refusal for one over-`MAX_CONTENT_LENGTH` body.

The ASGI transport refused an over-limit body by writing an
`http.response.start` straight to the server, before a `Request` existed - so
the rejection ran no response phase and carried none of the app's headers. A
`stream=True` route refused the same body from inside dispatch, so it did. One
app answered the same condition two ways depending on which kind of route the
client happened to hit, and with two different messages.

The rejection is now built by the one builder both paths share, on a real
`Request`, and every path states the same message.

What this does *not* do is run the request phase for a request being refused:
the declared-`Content-Length` check exists to refuse before reading a body, and
running middleware there would give that up. `CORSMiddleware` records the origin
in its request phase, so a rejection issued before dispatch carries no
`Access-Control-Allow-Origin` - that residual is pinned below rather than left
to be rediscovered.
"""

from __future__ import annotations

import pytest

from veloce import Request, Veloce
from veloce.middleware.base import Middleware
from veloce.testclient import TestClient

_LIMIT = 10
_OVER = b"x" * 50


class _Stamp(Middleware):
    """A response-phase header that needs no request-phase state."""

    async def process_response(self, request, response):
        response.headers["X-Stamped"] = "yes"
        return response


def _client(*, stream: bool, middleware: Middleware | None = None) -> TestClient:
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = _LIMIT
    if middleware is not None:
        app.add_middleware(middleware)

    if stream:

        async def streamed(request: Request):
            async for _chunk in request.stream():
                pass
            return {"ok": True}

        app.add_route("/u", streamed, methods=["POST"], stream=True)
    else:

        @app.post("/u")
        async def buffered(request: Request):
            return {"n": len(await request.body())}

    return TestClient(app)


# ── One message, whichever route kind received it ────────────────────


@pytest.mark.parametrize("stream", [False, True])
def test_an_over_limit_body_is_refused(stream):
    assert _client(stream=stream).post("/u", content=_OVER).status_code == 413


def test_both_route_kinds_state_the_same_reason():
    """The defect: two messages for one condition."""
    buffered = _client(stream=False).post("/u", content=_OVER).json()
    streamed = _client(stream=True).post("/u", content=_OVER).json()
    assert buffered["detail"] == streamed["detail"]
    assert buffered["status_code"] == streamed["status_code"] == 413


# ── The rejection runs the response phase ────────────────────────────


@pytest.mark.parametrize("stream", [False, True])
def test_the_rejection_carries_response_phase_headers(stream):
    """The defect: the ASGI refusal was written before any middleware ran."""
    response = _client(stream=stream, middleware=_Stamp()).post("/u", content=_OVER)
    assert response.status_code == 413
    assert response.headers.get("x-stamped") == "yes"


def test_a_declared_content_length_is_still_refused_before_the_body_is_read():
    """The early guard must stay early: refuse on the header, not after reading."""
    client = _client(stream=False, middleware=_Stamp())
    response = client.post("/u", content=_OVER, headers={"content-length": str(len(_OVER))})
    assert response.status_code == 413
    assert response.headers.get("x-stamped") == "yes"


def test_a_body_within_the_limit_is_unaffected():
    response = _client(stream=False, middleware=_Stamp()).post("/u", content=b"ok")
    assert response.status_code == 200
    assert response.json()["n"] == 2


# ── The residual, pinned so it is a decision and not a surprise ──────


def test_a_pre_dispatch_rejection_carries_no_cors_header():
    """`CORSMiddleware` needs its request phase, which a refusal does not run.

    Recorded deliberately: running the request middleware chain for a body we
    are refusing on its declared length would give up the point of refusing
    early. A `stream=True` route refuses from inside dispatch, so its request
    phase has already run and it does carry the header - that asymmetry is the
    cost of refusing before reading, not an oversight.
    """
    from veloce.middleware.cors import CORSMiddleware

    origin = {"origin": "https://site.example"}
    buffered = _client(
        stream=False, middleware=CORSMiddleware(allow_origins=["https://site.example"])
    ).post("/u", content=_OVER, headers=origin)
    streamed = _client(
        stream=True, middleware=CORSMiddleware(allow_origins=["https://site.example"])
    ).post("/u", content=_OVER, headers=origin)

    assert buffered.status_code == streamed.status_code == 413
    assert buffered.headers.get("access-control-allow-origin") is None
    assert streamed.headers.get("access-control-allow-origin") == "https://site.example"
