"""An `HTTPException` owns its headers; the raiser keeps its own mapping.

The mapping handed to `HTTPException` becomes the error response's `headers`,
which response middleware mutates in place. Sharing it with the raiser let one
request's `Set-Cookie` and `Access-Control-Allow-Origin` accumulate on a
caller-held dict and ship on every later raise.

A security scheme caching its `WWW-Authenticate` challenge once is the shape
that leaks - and caching it is the sensible thing to do, since the challenge is
request-invariant. `HTTPBasic` copied per raise and was safe; the `APIKey`
schemes passed the cached dict by reference and were not. The copy now happens
where the mapping enters the framework, so no scheme has to know the rule.
"""

from __future__ import annotations

import pytest

from veloce import Depends, HTTPException, Response, Veloce
from veloce.middleware.base import Middleware
from veloce.middleware.cors import CORSMiddleware
from veloce.security import APIKeyCookie, APIKeyHeader, APIKeyQuery, HTTPBasic
from veloce.testclient import TestClient

_ORIGINS = ["https://a.example", "https://b.example"]


# ── The chokepoint itself ────────────────────────────────────────────


def test_the_exception_copies_the_mapping_it_is_handed():
    caller_owned = {"WWW-Authenticate": "APIKey"}
    exc = HTTPException(401, "no", headers=caller_owned)
    assert exc.headers == caller_owned
    assert exc.headers is not caller_owned


def test_mutating_the_response_headers_cannot_reach_the_raiser():
    caller_owned = {"WWW-Authenticate": "APIKey"}
    exc = HTTPException(401, "no", headers=caller_owned)
    exc.headers["Set-Cookie"] = "session=leaked"
    assert caller_owned == {"WWW-Authenticate": "APIKey"}


def test_two_raises_from_one_cached_mapping_stay_independent():
    cached = {"WWW-Authenticate": "APIKey"}
    first = HTTPException(401, "no", headers=cached)
    second = HTTPException(401, "no", headers=cached)
    first.headers["Vary"] = "Origin"
    assert "Vary" not in second.headers
    assert "Vary" not in cached


def test_no_headers_still_yields_an_empty_mapping():
    assert HTTPException(401, "no").headers == {}
    assert HTTPException(401, "no", headers={}).headers == {}


# ── The reported leak, end to end ────────────────────────────────────


def _app(scheme, *, middleware) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(middleware)

    @app.get("/p")
    async def protected(cred=Depends(scheme)) -> dict:
        return {"ok": True}

    return app


@pytest.mark.parametrize(
    "scheme_factory",
    [
        lambda: APIKeyHeader(name="X-API-Key"),
        lambda: APIKeyQuery(name="api_key"),
        lambda: APIKeyCookie(name="api_key"),
        lambda: HTTPBasic(realm="r"),
    ],
)
def test_a_credential_less_request_gets_no_earlier_callers_cors_headers(scheme_factory):
    """The defect: the third request was served the second one's origin."""
    scheme = scheme_factory()
    client = TestClient(
        _app(
            scheme,
            middleware=CORSMiddleware(
                allow_origins=_ORIGINS,
                allow_headers=["X-API-Key", "Authorization"],
                allow_credentials=True,
            ),
        )
    )
    for origin in _ORIGINS:
        assert client.get("/p", headers={"Origin": origin}).status_code == 401

    no_origin = client.get("/p")
    assert no_origin.status_code == 401
    assert no_origin.headers.get("access-control-allow-origin") is None
    assert no_origin.headers.get("access-control-allow-credentials") is None


class _CookieStamper(Middleware):
    """Stands in for any middleware that attaches a per-request cookie."""

    def __init__(self) -> None:
        self._n = 0

    async def process_response(self, request, response: Response) -> Response:
        self._n += 1
        response.set_cookie("token", f"user-{self._n}")
        return response


@pytest.mark.parametrize(
    "scheme_factory",
    [lambda: APIKeyHeader(name="X-API-Key"), lambda: HTTPBasic(realm="r")],
)
def test_one_challenge_never_carries_an_earlier_requests_cookie(scheme_factory):
    """The sharper half of the leak: a per-request `Set-Cookie` on a shared dict."""
    client = TestClient(_app(scheme_factory(), middleware=_CookieStamper()))
    first = client.get("/p")
    second = client.get("/p")
    assert first.status_code == second.status_code == 401
    assert "user-1" in first.headers["set-cookie"]
    assert "user-1" not in second.headers["set-cookie"]
    assert "user-2" in second.headers["set-cookie"]


@pytest.mark.parametrize(
    ("scheme_factory", "attribute"),
    [
        (lambda: APIKeyHeader(name="X-API-Key"), "_challenge"),
        (lambda: APIKeyQuery(name="api_key"), "_challenge"),
        (lambda: APIKeyCookie(name="api_key"), "_challenge"),
        (lambda: HTTPBasic(realm="r"), "_challenge_template"),
    ],
)
def test_a_schemes_cached_challenge_stays_pristine(scheme_factory, attribute):
    """A scheme may cache its challenge; nothing downstream may write to it."""
    scheme = scheme_factory()
    before = dict(getattr(scheme, attribute))
    client = TestClient(
        _app(
            scheme,
            middleware=CORSMiddleware(
                allow_origins=_ORIGINS,
                allow_headers=["X-API-Key", "Authorization"],
                allow_credentials=True,
            ),
        )
    )
    for origin in _ORIGINS:
        client.get("/p", headers={"Origin": origin})
    client.get("/p")
    assert getattr(scheme, attribute) == before


def test_the_challenge_still_reaches_the_client():
    """Isolation must not cost the response its `WWW-Authenticate`."""
    client = TestClient(
        _app(APIKeyHeader(name="X-API-Key"), middleware=CORSMiddleware(allow_origins=_ORIGINS))
    )
    response = client.get("/p")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "APIKey"
