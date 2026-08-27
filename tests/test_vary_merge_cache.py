"""`add_vary` memoises its merge without changing what it produces.

The fast path only fires when no `Vary` exists yet, so in a stack where CORS,
compression and sessions each contribute one name, the second and third calls
parsed and re-serialised the whole list on every response - 5.3 us against
0.5 us for the first.

The merge is a pure function of `(existing_value, names_being_added)` and an
application's middleware order is fixed, so the same two questions are asked on
every response. What a cache must not change is the answer, and what it must not
do is grow without bound: a handler can write `response.headers["Vary"]` from
user input.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.http.response import _MAX_VARY_MERGES, _VARY_MERGES, Response
from veloce.middleware.security import SecurityHeadersMiddleware
from veloce.testclient import TestClient


@pytest.fixture(autouse=True)
def _clear_cache():
    _VARY_MERGES.clear()
    yield
    _VARY_MERGES.clear()


def _response() -> Response:
    return Response(body=b"x", content_type="text/plain")


# ── the answer is unchanged ──────────────────────────────────────────

CASES = [
    pytest.param(["Cookie"], "Cookie", id="single"),
    pytest.param(["Origin", "Accept-Encoding"], "Origin, Accept-Encoding", id="two-calls"),
    pytest.param(
        ["Origin", "Accept-Encoding", "Cookie"],
        "Origin, Accept-Encoding, Cookie",
        id="the-real-stack",
    ),
    pytest.param(["Cookie", "Cookie"], "Cookie", id="duplicate-collapses"),
    pytest.param(["Cookie", "cookie"], "Cookie", id="duplicate-is-case-insensitive"),
    pytest.param(["Accept", "Accept-Encoding"], "Accept, Accept-Encoding", id="prefix-not-a-match"),
]


@pytest.mark.parametrize(("names", "expected"), CASES)
def test_the_merged_value_is_what_it_always_was(names: list[str], expected: str):
    response = _response()
    for name in names:
        response.add_vary(name)
    assert response.headers["Vary"] == expected


@pytest.mark.parametrize(("names", "expected"), CASES)
def test_a_warm_cache_produces_the_same_value_as_a_cold_one(names: list[str], expected: str):
    first = _response()
    for name in names:
        first.add_vary(name)
    second = _response()
    for name in names:
        second.add_vary(name)
    assert first.headers["Vary"] == second.headers["Vary"] == expected


def test_adding_several_names_at_once_still_merges():
    response = _response()
    response.add_vary("Origin")
    response.add_vary("Accept-Encoding", "Cookie")
    assert response.headers["Vary"] == "Origin, Accept-Encoding, Cookie"


def test_an_existing_header_in_another_casing_is_replaced_not_duplicated():
    """The reason the scan looks at every casing; a cache must not undo it."""
    response = _response()
    response.headers["VARY"] = "Cookie"
    response.add_vary("Origin")
    keys = [k for k in response.headers if k.lower() == "vary"]
    assert keys == ["Vary"]
    assert response.headers["Vary"] == "Cookie, Origin"


def test_the_return_value_matches_the_stored_header():
    response = _response()
    response.add_vary("Origin")
    returned = response.add_vary("Cookie")
    assert returned == response.headers["Vary"] == "Origin, Cookie"


def test_a_handler_supplied_vary_is_merged_not_trusted_blindly():
    response = _response()
    response.headers["Vary"] = "User-Agent"
    assert response.add_vary("Cookie") == "User-Agent, Cookie"


# ── the cache is real, and bounded ───────────────────────────────────


def test_the_vary_cache_is_actually_used():
    """A memo that never hits would make the whole change pointless."""
    first = _response()
    first.add_vary("Origin")
    first.add_vary("Cookie")
    assert len(_VARY_MERGES) == 1
    second = _response()
    second.add_vary("Origin")
    second.add_vary("Cookie")
    assert len(_VARY_MERGES) == 1


def test_the_fast_path_never_populates_the_cache():
    """A single name onto an empty header needs no merge at all."""
    _response().add_vary("Cookie")
    assert _VARY_MERGES == {}


def test_a_per_response_vary_cannot_grow_the_cache_without_bound():
    """A handler may build `Vary` from user input; that must not accumulate."""
    for i in range(_MAX_VARY_MERGES * 3):
        response = _response()
        response.headers["Vary"] = f"X-Tenant-{i}"
        response.add_vary("Cookie")
    assert len(_VARY_MERGES) <= _MAX_VARY_MERGES


def test_the_vary_cache_keeps_working_after_it_is_cleared():
    for i in range(_MAX_VARY_MERGES * 2):
        response = _response()
        response.headers["Vary"] = f"X-{i}"
        response.add_vary("Cookie")
    response = _response()
    response.add_vary("Origin")
    assert response.add_vary("Cookie") == "Origin, Cookie"


# ── end to end ───────────────────────────────────────────────────────


def test_repeated_requests_carry_the_same_vary():
    app = Veloce(openapi_url=None)
    app.add_middleware(SecurityHeadersMiddleware(hsts_max_age=31536000))

    @app.get("/j")
    async def j() -> dict:
        return {"ok": True}

    client = TestClient(app)
    first = client.get("/j").headers.get("vary")
    second = client.get("/j").headers.get("vary")
    assert first == second
