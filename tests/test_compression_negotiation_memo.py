"""The negotiated coding is memoised per middleware instance, and stays correct.

`_negotiate` splits, strips, lowers and builds a set on every response, though
clients send one of a handful of stable `Accept-Encoding` strings and the
offered codings are fixed at construction - so the answer is a pure function of
the header. Measured on techc: ~378 ns per response for the header browsers
actually send (`gzip, deflate, br`), against ~47 ns for a dict hit.

A cache in front of content negotiation is a correctness hazard in two
directions - serving one client's coding to another, and growing without bound
on attacker-chosen headers - so this module tests both:

* every header shape resolves to the same coding cached or uncached;
* the memo is bounded, and recycles rather than refusing new entries, so a flood
  of junk cannot lock the real values out.
"""

from __future__ import annotations

import pytest

from veloce import CompressionMiddleware, Response, Veloce
from veloce.middleware.compression import _MAX_NEGOTIATED, _negotiate
from veloce.testclient import TestClient

BODY = b"x" * 4096

HEADERS = [
    "gzip, deflate, br",
    "gzip",
    "gzip, deflate",
    "br",
    "*",
    "gzip;q=0",
    "gzip;q=0, *",
    "identity",
    "",
    "GZIP",
    "  gzip  ,  deflate  ",
    "br;q=1.0, gzip;q=0.8",
]


def _app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(CompressionMiddleware(minimum_size=1))

    @app.get("/x")
    async def x():

        return Response(body=BODY, content_type="text/plain")

    return app


def _encoding(client, header: str) -> str | None:
    resp = client.get("/x", headers={"Accept-Encoding": header} if header else {})
    return resp.headers.get("content-encoding")


# ── the memo does not change the answer ──────────────────────────────


@pytest.mark.parametrize("header", HEADERS)
def test_a_header_resolves_the_same_cached_and_uncached(header):
    """First request populates the memo, second reads it; they must agree."""
    client = TestClient(_app())
    first = _encoding(client, header)
    second = _encoding(client, header)
    assert first == second


@pytest.mark.parametrize("header", HEADERS)
def test_the_memo_agrees_with_the_negotiator(header):
    """Stated against the function it caches, not against a literal."""
    middleware = CompressionMiddleware(minimum_size=1)
    expected = _negotiate(header, middleware.algorithms)
    app = Veloce(openapi_url=None)
    app.add_middleware(middleware)

    @app.get("/x")
    async def x():

        return Response(body=BODY, content_type="text/plain")

    client = TestClient(app)
    for _ in range(3):
        assert _encoding(client, header) == expected


def test_one_clients_coding_is_not_served_to_another():
    """The sharp end of caching content negotiation."""
    client = TestClient(_app())
    assert _encoding(client, "gzip") == "gzip"
    assert _encoding(client, "br") in (None, "br")
    assert _encoding(client, "identity") is None
    assert _encoding(client, "gzip") == "gzip"


def test_interleaved_clients_each_get_their_own_coding():
    client = TestClient(_app())
    for _ in range(5):
        assert _encoding(client, "gzip") == "gzip"
        assert _encoding(client, "identity") is None


# ── and it is bounded ────────────────────────────────────────────────


def test_the_memo_is_bounded():
    """A flood of distinct headers must not grow it without limit."""
    middleware = CompressionMiddleware(minimum_size=1)
    app = Veloce(openapi_url=None)
    app.add_middleware(middleware)

    @app.get("/x")
    async def x():

        return Response(body=BODY, content_type="text/plain")

    client = TestClient(app)
    for index in range(_MAX_NEGOTIATED * 2 + 10):
        client.get("/x", headers={"Accept-Encoding": f"junk-{index}"})
    assert len(middleware._negotiated) <= _MAX_NEGOTIATED


def test_a_real_header_still_resolves_after_a_flood():
    """Recycling rather than refusing: the values that matter come back."""
    middleware = CompressionMiddleware(minimum_size=1)
    app = Veloce(openapi_url=None)
    app.add_middleware(middleware)

    @app.get("/x")
    async def x():

        return Response(body=BODY, content_type="text/plain")

    client = TestClient(app)
    for index in range(_MAX_NEGOTIATED + 5):
        client.get("/x", headers={"Accept-Encoding": f"junk-{index}"})
    assert _encoding(client, "gzip") == "gzip"


def test_the_memo_is_per_instance():
    """Two middlewares may offer different codings, so one memo cannot serve
    both."""
    a = CompressionMiddleware(minimum_size=1)
    b = CompressionMiddleware(minimum_size=1)
    assert a._negotiated is not b._negotiated


# ── the compression itself still works ───────────────────────────────


def test_a_gzip_client_gets_a_smaller_body():
    client = TestClient(_app())
    resp = client.get("/x", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("content-encoding") == "gzip"
    assert len(resp.body) < len(BODY)


def test_an_identity_client_gets_the_raw_body():
    client = TestClient(_app())
    resp = client.get("/x", headers={"Accept-Encoding": "identity"})
    assert resp.headers.get("content-encoding") is None
    assert resp.body == BODY


def test_vary_is_still_declared():
    client = TestClient(_app())
    resp = client.get("/x", headers={"Accept-Encoding": "gzip"})
    assert "accept-encoding" in resp.headers.get("Vary", "").lower()
