"""The two ASGI emit paths apply the same bodiless-status rules.

`_asgi_app`'s buffered branch and `AsgiMixin._emit_response` are deliberately
separate - one is the inline hot path against precomputed caches, the other the
cold reject path that carries a response's own headers. The split is intentional
and stays; the *rules* diverging is the problem.

The cold copy applied `if not has_ct:` where the hot one applies
`if not has_ct and body_allowed:`, so it would default a `content-type` onto a
status that may carry no payload (RFC 9110 Sec. 15.3.5 / 15.3.6 / 15.4.5) and
emit the body besides.

**No current caller reaches it.** Every path into `_emit_response` today - the
413 and `_refuse_bad_request` via `_http_exception_response` - uses a
body-permitting status, so this was
latent rather than live, and these tests say so rather than implying a bug that
could be observed. What makes it worth closing is that this is the general
"emit an already-built `Response`" helper: the next caller should not have to
know the two branches disagreed.

These tests drive `_emit_response` directly, which is the only way to reach the
bodiless case at all.
"""

from __future__ import annotations

import pytest

from veloce import Response, Veloce
from veloce import status as status_mod
from veloce.app.asgi import _emit_stream
from veloce.http.response import StreamingResponse
from veloce.testclient import TestClient

BODILESS = [204, 205, 304, 100, 102]
WITH_BODY = [200, 201, 400, 404, 413, 500]


async def _emit(status_code: int, *, body: bytes = b"payload", headers: dict | None = None):
    """Run `_emit_response` and return the ASGI messages it sent."""
    app = Veloce(openapi_url=None)
    response = Response(body=body, status_code=status_code, headers=headers or {})
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await app._emit_response(send, response)
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = next(m for m in sent if m["type"] == "http.response.body")
    return {k.decode(): v.decode() for k, v in start["headers"]}, payload.get("body", b"")


# ── a bodiless status gets neither a default content-type nor a body ──


@pytest.mark.parametrize("status_code", BODILESS)
async def test_a_bodiless_status_gets_no_default_content_type(status_code):
    headers, _body = await _emit(status_code)
    assert "content-type" not in headers


@pytest.mark.parametrize("status_code", BODILESS)
async def test_a_bodiless_status_sends_no_body(status_code):
    _headers, body = await _emit(status_code)
    assert body == b""


@pytest.mark.parametrize("status_code", [code for code in BODILESS if code != 304])
async def test_a_bodiless_status_reports_zero_length(status_code):
    """1xx, 204 and 205 have no representation, so their length is 0."""
    headers, _body = await _emit(status_code)
    assert headers["content-length"] == "0"


async def test_a_304_advertises_the_length_the_200_would_have():
    """The third rule this helper was out of step on.

    RFC 9110 Sec. 8.6 / 15.4.5: a 304 sends no body but may carry the
    `Content-Length` of the representation it is telling the client to reuse.
    `Response.encode` and the buffered ASGI branch both advertise it; this path
    advertised `0`, so the same response described itself two ways depending on
    which emit it went through.
    """
    headers, body = await _emit(304, body=b"payload")
    assert body == b""
    assert headers["content-length"] == str(len(b"payload"))


# ── and a body-permitting status is unaffected ───────────────────────
#
# The negative: suppressing the content-type everywhere would pass every
# assertion above and break every refusal this helper actually serves.


@pytest.mark.parametrize("status_code", WITH_BODY)
async def test_a_body_permitting_status_keeps_its_content_type(status_code):
    headers, body = await _emit(status_code)
    assert "content-type" in headers
    assert body == b"payload"


@pytest.mark.parametrize("status_code", WITH_BODY)
async def test_a_body_permitting_status_reports_its_length(status_code):
    headers, _body = await _emit(status_code)
    assert headers["content-length"] == str(len(b"payload"))


async def test_an_explicit_content_type_survives_on_a_bodiless_status():
    """`has_ct` wins: only the *default* is suppressed, not a handler's choice."""
    headers, _body = await _emit(204, headers={"Content-Type": "application/problem+json"})
    assert headers["content-type"] == "application/problem+json"


async def test_response_headers_are_still_carried():
    """The reason this path exists at all: a refusal that ran the response phase
    must ship its CORS and security headers."""
    headers, _body = await _emit(413, headers={"Vary": "Origin", "X-Trace": "1"})
    assert headers["vary"] == "Origin"
    assert headers["x-trace"] == "1"


# ── the two branches agree ───────────────────────────────────────────


@pytest.mark.parametrize("status_code", BODILESS + WITH_BODY)
def test_the_buffered_branch_applies_the_same_rule(status_code):
    """Stated against the other implementation rather than restated by hand."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(body=b"payload", status_code=status_code)

    with TestClient(app) as client:
        resp = client.get("/x")

    permits = status_mod.status_permits_body(status_code)
    assert ("content-type" in {k.lower() for k in resp.headers}) is permits


# ── the refusals that actually use this path still work ──────────────


def test_an_over_limit_body_is_still_refused_with_a_body():
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = 10

    @app.post("/p")
    async def p(request):
        return {"n": len(await request.body())}

    resp = TestClient(app).post("/p", content=b"x" * 500)
    assert resp.status_code == 413
    assert resp.body
    assert "content-type" in {k.lower() for k in resp.headers}


async def test_a_malformed_query_string_is_still_a_400_with_a_body():
    """The refusal this helper's live caller actually emits.

    Driven against the ASGI app directly: the query string has to carry raw
    non-ASCII bytes to reach `_refuse_bad_request`, and the test client builds
    a well-formed one. This test used to send an ordinary request and assert
    `200`, so it exercised nothing its name describes.
    """
    app = Veloce(openapi_url=None)

    @app.get("/q")
    async def q():
        return {"ok": True}

    await app._run_lifecycle("startup")
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "path": "/q",
            # Raw non-ASCII bytes, built explicitly so the source stays ASCII.
            "query_string": bytes([0xFF, 0xFE]) + b"=1",
            "headers": [],
            "scheme": "http",
            "server": ("t", 80),
            "client": ("c", 1),
            "root_path": "",
        },
        receive,
        send,
    )
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert start["status"] == 400
    assert body, "a 400 permits a body, so the refusal must carry one"


# ── a streamed response survives the cold emit path ──────────────────


async def _drive_stream(status_code: int = 400, *, method: str | None = None):
    """Run `_emit_response` on a streaming response and collect the messages."""
    app = Veloce(openapi_url=None)

    async def chunks():
        yield b"CHUNK-ONE"
        yield b"CHUNK-TWO"

    response = StreamingResponse(chunks(), status_code=status_code)
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await app._emit_response(send, response, method)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return {k.decode(): v.decode() for k, v in start["headers"]}, body


async def test_a_streamed_response_is_emitted_not_dropped():
    """A refusal runs the app's error handlers, which may return a stream.

    The cold path sent only `response.body`, so a handler returning a
    `StreamingResponse` reached the client as an empty body - and a
    `FileResponse` as its real `Content-Length` with no bytes behind it, which
    desynchronises a keep-alive connection.
    """
    headers, body = await _drive_stream()
    assert body == b"CHUNK-ONECHUNK-TWO"
    assert headers.get("content-length") is None


async def test_a_streamed_response_to_head_sends_headers_only():
    _headers, body = await _drive_stream(method="HEAD")
    assert body == b""


@pytest.mark.parametrize("status_code", BODILESS)
async def test_a_streamed_bodiless_status_sends_no_chunks(status_code):
    """The rule the hot path applies: a bodiless status ships no payload."""
    headers, body = await _drive_stream(status_code)
    assert body == b""
    assert "content-type" not in headers


# ── one writer for both streaming paths ───────────────────────────────


def test_a_streamed_response_keeps_its_own_content_type():
    """The hot path discarded `has_ct` and appended the framework default too.

    Two `Content-Type` headers reached the client and most take the second, so
    a `text/csv` download arrived as `application/octet-stream`. The cold path
    already read the flag; this is what the two copies diverging looked like.
    """
    app = Veloce(openapi_url=None)

    @app.get("/csv")
    async def csv() -> Response:
        async def chunks():
            yield b"a,b\n"

        return StreamingResponse(chunks(), headers={"Content-Type": "text/csv"})

    client = TestClient(app)
    resp = client.get("/csv")
    assert resp.headers["content-type"] == "text/csv"


@pytest.mark.parametrize("status_code", BODILESS)
def test_a_streamed_bodiless_status_ships_no_chunks_through_dispatch(status_code):
    """The rule the hot copy once got wrong, now pinned on the dispatch path.

    A streamed `204` shipped its chunks, and a client on a keep-alive
    connection read them as the start of the next response.
    """
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s() -> Response:
        async def chunks():
            yield b"NOPE"

        return StreamingResponse(chunks(), status_code=status_code)

    resp = TestClient(app).get("/s")
    assert resp.status_code == status_code
    assert resp.text == ""


def test_a_streamed_head_response_sends_headers_only_through_dispatch():
    """The hot path reads `scope["method"]`, the cold one a parameter.

    One writer has to get the same answer from both, so a refused `HEAD` and a
    dispatched `HEAD` agree.
    """
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s() -> Response:
        async def chunks():
            yield b"BODY"

        return StreamingResponse(chunks())

    client = TestClient(app)
    assert client.get("/s").text == "BODY"
    assert client.head("/s").text == ""


def test_a_streamed_response_keeps_a_content_length_it_set():
    """A file's length is known from its stat, so it stays length-delimited.

    Dropping it would silently turn a sized download into a chunked one.
    """
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s() -> Response:
        async def chunks():
            yield b"0123456789"

        return StreamingResponse(chunks(), headers={"Content-Length": "10"})

    resp = TestClient(app).get("/s")
    assert resp.headers["content-length"] == "10"


@pytest.mark.parametrize(
    "status_code,method",
    [
        (200, "GET"),
        (204, "GET"),
        (304, "GET"),
        (102, "GET"),
        (200, "HEAD"),
        (204, "HEAD"),
        (200, None),
    ],
    ids=["200-get", "204", "304", "102", "200-head", "204-head", "no-method"],
)
async def test_every_streamed_path_sends_exactly_one_terminal_frame(status_code, method):
    """An ASGI server that never receives `more_body: False` hangs the connection.

    The chunk loop is skipped on a bodiless status and on `HEAD`, so the
    terminal frame has to sit outside it - which is easy to get wrong when the
    early returns are written out per call site, as they were.
    """

    async def chunks():
        yield b"CHUNK"

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await _emit_stream(send, StreamingResponse(chunks(), status_code=status_code), method)
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert len([m for m in bodies if not m.get("more_body", False)]) == 1


async def test_an_empty_stream_sends_only_the_terminal_frame():
    """A generator that yields nothing is a valid empty body, not a hang.

    The chunk loop runs zero times, so the terminal `more_body: False` frame is
    the only body message - which is what tells the server the response is
    complete.
    """

    async def chunks():
        return
        yield b""  # unreachable; makes this an async generator

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await _emit_stream(send, StreamingResponse(chunks()), "GET")
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert len(bodies) == 1
    assert bodies[0].get("more_body", False) is False
    assert bodies[0].get("body", b"") == b""
