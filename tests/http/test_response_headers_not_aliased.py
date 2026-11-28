"""A `Response` copies the headers dict it is given, so one request cannot
write into a caller-held mapping that a later request will ship.

`Response.headers` is mutated in place all through the response phase -
`set_cookie`, the `Content-Length` setter, the session / CORS / security-header
middleware. Aliasing the caller's dict therefore hands that whole stack a
writable handle on it.

The shape that leaks is ordinary, not exotic: a module-level header constant
reused by a few routes.

    COMMON = {"X-App-Version": "1.0"}

    @app.get("/login")
    async def login(request):
        request.session["user"] = ...
        return JSONResponse({...}, headers=COMMON)

The session middleware writes `Set-Cookie` into `COMMON` while serving one
request; the next request starts from a dict that already contains it and sends
it. What leaks is a signed session cookie belonging to a different user, and it
accumulates one entry per request forever.

`HTTPException.__init__` already carries this rule and the reasoning for it.
Only the error path had it; the success path is the far more common one.
"""

from __future__ import annotations

import pytest

from veloce import JSONResponse, Response, Veloce
from veloce.middleware.security import SecurityHeadersMiddleware
from veloce.middleware.sessions import SessionMiddleware
from veloce.testclient import TestClient

SECRET = "k" * 32


# ── the constructor does not alias ───────────────────────────────────


def test_the_constructor_copies_the_mapping_it_is_given():
    supplied = {"X-App-Version": "1.0"}
    response = Response(body=b"x", headers=supplied)
    assert response.headers == supplied
    assert response.headers is not supplied


def test_mutating_the_response_does_not_touch_the_callers_dict():
    supplied = {"X-App-Version": "1.0"}
    response = Response(body=b"x", headers=supplied)
    response.headers["X-Added"] = "yes"
    response.set_cookie("session", "abc")
    assert supplied == {"X-App-Version": "1.0"}


def test_a_json_response_copies_too():
    """The subclass every handler actually returns."""
    supplied = {"X-App-Version": "1.0"}
    response = JSONResponse({"ok": True}, headers=supplied)
    response.headers["X-Added"] = "yes"
    assert supplied == {"X-App-Version": "1.0"}


def test_no_headers_still_gets_a_fresh_dict_each_time():
    """The common path must not start sharing one empty dict."""
    first = Response(body=b"x")
    second = Response(body=b"y")
    first.headers["X-Only-Mine"] = "1"
    assert second.headers == {}
    assert first.headers is not second.headers


def test_an_empty_dict_is_not_aliased_either():
    supplied: dict[str, str] = {}
    response = Response(body=b"x", headers=supplied)
    response.headers["X-Added"] = "yes"
    assert supplied == {}


# ── end to end: the actual leak ──────────────────────────────────────


def _app() -> tuple[Veloce, dict[str, str]]:
    common = {"X-App-Version": "1.0"}
    app = Veloce(openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key=SECRET))

    @app.get("/login")
    async def login(request) -> JSONResponse:
        request.session["user"] = request.query_params.get("u", "anon")
        return JSONResponse({"ok": True}, headers=common)

    return app, common


def test_one_users_session_cookie_never_reaches_another_users_response():
    """The defect: bob's response carried alice's signed session cookie."""
    app, _common = _app()

    alice = TestClient(app).get("/login?u=alice")
    alice_cookie = alice.headers.get("set-cookie", "")
    assert "session=" in alice_cookie
    alice_token = alice_cookie.split("session=", 1)[1].split(";", 1)[0]

    # A separate client: a different browser, an empty cookie jar.
    bob = TestClient(app).get("/login?u=bob")
    bob_cookie = bob.headers.get("set-cookie", "")

    assert alice_token not in bob_cookie, "alice's session token reached bob's response"
    assert bob_cookie.count("session=") == 1


def test_the_handlers_header_constant_is_not_written_to():
    app, common = _app()
    client = TestClient(app)
    client.get("/login?u=alice")
    client.get("/login?u=bob")
    assert common == {"X-App-Version": "1.0"}


def test_the_supplied_header_is_still_delivered():
    """The copy must not stop the caller's headers reaching the wire."""
    app, _common = _app()
    assert TestClient(app).get("/login?u=alice").headers["x-app-version"] == "1.0"


def test_repeated_requests_do_not_accumulate_set_cookie():
    """It grew by one entry per request, so this is also a memory leak."""
    app, common = _app()
    client = TestClient(app)
    for _ in range(5):
        client.get("/login?u=alice")
    assert list(common) == ["X-App-Version"]


@pytest.mark.parametrize(
    ("middleware", "writes_session"),
    [
        pytest.param(SessionMiddleware(secret_key=SECRET), True, id="session"),
        pytest.param(
            SecurityHeadersMiddleware(hsts_max_age=31536000), False, id="security-headers"
        ),
    ],
)
def test_no_response_middleware_writes_into_the_callers_dict(middleware, writes_session):
    """Any middleware that adds a response header is the same hazard."""
    common = {"X-App-Version": "1.0"}
    app = Veloce(openapi_url=None)
    app.add_middleware(middleware)

    @app.get("/x")
    async def x(request) -> JSONResponse:
        # Touch the session only where one exists, so the same test body covers
        # a cookie-writing middleware and a header-only one.
        if writes_session:
            request.session["u"] = "a"
        return JSONResponse({"ok": True}, headers=common)

    TestClient(app).get("/x")
    assert common == {"X-App-Version": "1.0"}
