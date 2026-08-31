"""All three over-limit refusal points answer the same way.

`MAX_CONTENT_LENGTH` can be tripped at three points on the ASGI path:

1. the **declared** `Content-Length`, refused before a body byte is read;
2. the **running total** of a buffered read, for a body that omits or understates
   its length;
3. the **running total** of a streamed read, for a `stream=True` route.

Each built the same throwaway `Request` and emitted the same response, written
out three times inside one function. They now share one `_refuse_too_large`.

The consolidation is the point, but so is the test: a refusal path written out
per site is exactly how one gets fixed and the others do not — which is what
happened to the native transport's own 413, reported separately in this same
review. These tests assert the three against each other, so a change that
reaches one and not the others fails here.
"""

from __future__ import annotations

import pytest

from veloce import CORSMiddleware, Veloce
from veloce.testclient import TestClient

_LIMIT = 100
_ORIGIN = "https://ok.example"


def _app(*, cors: bool = False) -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = _LIMIT
    if cors:
        app.add_middleware(CORSMiddleware(allow_origins=[_ORIGIN]))

    @app.post("/buffered")
    async def buffered(request):
        return {"n": len(await request.body())}

    @app.post("/streamed", stream=True)
    async def streamed(request):
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        return {"n": total}

    return app


def _declared(client, path="/buffered"):
    """Over-limit `Content-Length`, refused before the body is read."""
    return client.post(path, content=b"x" * (_LIMIT * 5))


def _chunked(client, path):
    """No declared length, so only the running total can catch it."""
    return client.post(path, stream=[b"x" * 60, b"x" * 60, b"x" * 60])


# ── every refusal point refuses ──────────────────────────────────────


def test_a_declared_over_limit_body_is_refused():
    assert _declared(TestClient(_app())).status_code == 413


def test_an_undeclared_over_limit_body_is_refused_on_the_buffered_path():
    assert _chunked(TestClient(_app()), "/buffered").status_code == 413


def test_an_undeclared_over_limit_body_is_refused_on_the_streamed_path():
    assert _chunked(TestClient(_app()), "/streamed").status_code == 413


# ── and they refuse identically ──────────────────────────────────────


def test_all_three_refusals_share_a_status():
    client = TestClient(_app())
    statuses = {
        _declared(client).status_code,
        _chunked(client, "/buffered").status_code,
        _chunked(client, "/streamed").status_code,
    }
    assert statuses == {413}


def test_all_three_refusals_share_a_body():
    """The payload names the limit; all three must name the same one."""
    client = TestClient(_app())
    bodies = {
        _declared(client).body,
        _chunked(client, "/buffered").body,
        _chunked(client, "/streamed").body,
    }
    assert len(bodies) == 1, bodies


def test_the_refusal_names_the_limit():
    assert str(_LIMIT).encode() in _declared(TestClient(_app())).body


def test_all_three_refusals_share_a_content_type():
    client = TestClient(_app())
    types = {
        _declared(client).headers["content-type"],
        _chunked(client, "/buffered").headers["content-type"],
        _chunked(client, "/streamed").headers["content-type"],
    }
    assert len(types) == 1, types


@pytest.mark.parametrize(
    ("refuse", "path"),
    [(_declared, "/buffered"), (_chunked, "/buffered"), (_chunked, "/streamed")],
    ids=["declared", "buffered-total", "streamed-total"],
)
def test_every_refusal_runs_the_response_phase(refuse, path):
    """The reason the throwaway `Request` is built at all: a cross-origin upload
    that trips the limit must reach the client as a 413, not as an opaque CORS
    failure."""
    client = TestClient(_app(cors=True))
    resp = refuse(client, path) if refuse is _chunked else refuse(client, path)
    assert resp.status_code == 413
    assert resp.headers.get("Vary") == "Origin"


# ── an under-limit body is served, on every path ─────────────────────
#
# The negatives: a limit that refused everything would be worse than one that
# refused nothing.


def test_an_under_limit_declared_body_is_served():
    assert TestClient(_app()).post("/buffered", content=b"x" * 10).json() == {"n": 10}


def test_an_under_limit_chunked_body_is_served_buffered():
    client = TestClient(_app())
    assert client.post("/buffered", stream=[b"x" * 10, b"x" * 10]).json() == {"n": 20}


def test_an_under_limit_chunked_body_is_served_streamed():
    client = TestClient(_app())
    assert client.post("/streamed", stream=[b"x" * 10, b"x" * 10]).json() == {"n": 20}


def test_a_body_exactly_at_the_limit_is_served():
    assert TestClient(_app()).post("/buffered", content=b"x" * _LIMIT).json() == {"n": _LIMIT}


def test_an_empty_body_is_served():
    assert TestClient(_app()).post("/buffered", content=b"").json() == {"n": 0}


def test_no_limit_configured_serves_a_large_body():
    """`MAX_CONTENT_LENGTH = None` disables the check on every path."""
    app = _app()
    app.config["MAX_CONTENT_LENGTH"] = None
    assert TestClient(app).post("/buffered", content=b"x" * 5000).json() == {"n": 5000}
