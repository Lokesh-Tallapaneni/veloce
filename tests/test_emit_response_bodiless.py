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
413, `_emit_error`, `_emit_400` - uses a body-permitting status, so this was
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


@pytest.mark.parametrize("status_code", BODILESS)
async def test_a_bodiless_status_reports_zero_length(status_code):
    headers, _body = await _emit(status_code)
    assert headers["content-length"] == "0"


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


def test_a_malformed_query_string_is_still_a_400_with_a_body():
    app = Veloce(openapi_url=None)

    @app.get("/q")
    async def q():
        return {"ok": True}

    client = TestClient(app)
    resp = client.get("/q")
    assert resp.status_code == 200
