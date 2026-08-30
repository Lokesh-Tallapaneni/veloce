"""A refused request still carries the headers the response phase adds.

`_emit_response`'s docstring says the cold reject path exists so that "a
rejection that ran the response phase [can] ship its CORS and security headers,
rather than reaching the client as an opaque cross-origin failure with no
`Access-Control-Allow-Origin` on it". It did run the response phase - against a
throwaway `Request` built just for it, which never went through the *request*
phase.

`CORSMiddleware` reads the origin from `request._state["_cors_origin"]`, a cache
its `process_request` writes. On the refusal path that key is absent, so the
origin read as `""`, no origin was allowed, and the 413 went out bare: the
browser reports a CORS failure and the status saying what was actually wrong is
unreadable.

A response-phase middleware has to work on a request that skipped the request
phase, because refusals are exactly that.
"""

from __future__ import annotations

import pytest

from veloce import CORSMiddleware, SecurityHeadersMiddleware, Veloce
from veloce.testclient import TestClient

ORIGIN = "https://ok.example"
ALLOW = "access-control-allow-origin"


def _app(limit: int | None = 10) -> Veloce:
    app = Veloce(openapi_url=None)
    if limit is not None:
        app.config["MAX_CONTENT_LENGTH"] = limit
    app.add_middleware(CORSMiddleware(allow_origins=[ORIGIN]))

    @app.post("/p")
    async def p():
        return {"ok": True}

    return app


def test_an_accepted_request_carries_the_cors_header():
    """The control: without it, the test below could pass on a broken app."""
    resp = TestClient(_app()).post("/p", content=b"tiny", headers={"Origin": ORIGIN})

    assert resp.status_code == 200
    assert resp.headers.get(ALLOW) == ORIGIN


def test_an_over_limit_refusal_carries_it_too():
    """The regression: the 413 went out with no `Access-Control-Allow-Origin`."""
    resp = TestClient(_app()).post("/p", content=b"x" * 500, headers={"Origin": ORIGIN})

    assert resp.status_code == 413
    assert resp.headers.get(ALLOW) == ORIGIN, (
        "the refusal reaches a browser as an opaque CORS failure, so the client "
        "never sees the 413 that says what was wrong"
    )


def test_the_refusal_still_says_what_the_limit_was():
    """The fix must not cost the payload the refusal exists to deliver."""
    resp = TestClient(_app()).post("/p", content=b"x" * 500, headers={"Origin": ORIGIN})

    assert resp.json()["status_code"] == 413
    assert resp.json()["limit"] == 10


def test_a_disallowed_origin_is_still_refused_on_the_413():
    """Falling back to the header must not start allowing everything."""
    resp = TestClient(_app()).post(
        "/p", content=b"x" * 500, headers={"Origin": "https://evil.example"}
    )

    assert resp.status_code == 413
    assert resp.headers.get(ALLOW) is None


def test_a_request_with_no_origin_gets_no_allow_header():
    resp = TestClient(_app()).post("/p", content=b"x" * 500)

    assert resp.status_code == 413
    assert resp.headers.get(ALLOW) is None


def test_security_headers_reach_the_refusal_as_well():
    """CORS is the reported case; the docstring's claim is about the phase."""
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = 10
    app.add_middleware(SecurityHeadersMiddleware())

    @app.post("/p")
    async def p():
        return {"ok": True}

    resp = TestClient(app).post("/p", content=b"x" * 500)

    assert resp.status_code == 413
    assert resp.headers.get("x-content-type-options") == "nosniff"


@pytest.mark.parametrize("size", [11, 500, 5000], ids=["just-over", "over", "well-over"])
def test_every_refusal_size_behaves_the_same(size):
    """The body is refused at more than one point; all of them emit this way."""
    resp = TestClient(_app()).post("/p", content=b"x" * size, headers={"Origin": ORIGIN})

    assert resp.status_code == 413
    assert resp.headers.get(ALLOW) == ORIGIN
