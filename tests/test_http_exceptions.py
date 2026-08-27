"""Per-code HTTPException subclasses + MRO handler lookup (E2, E3)."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import HTTPException, JSONResponse, Veloce
from veloce.exceptions import (
    BadRequest,
    Conflict,
    Forbidden,
    InternalServerError,
    MethodNotAllowed,
    NotFound,
    ServiceUnavailable,
    TooManyRequests,
    Unauthorized,
    UnprocessableEntity,
    exception_for_status,
)
from veloce.helpers import abort

# ── Subclass identities ────────────────────────────────────────────────


def test_subclasses_carry_correct_codes():
    assert BadRequest().status_code == 400
    assert Unauthorized().status_code == 401
    assert Forbidden().status_code == 403
    assert NotFound().status_code == 404
    assert MethodNotAllowed().status_code == 405
    assert Conflict().status_code == 409
    assert UnprocessableEntity().status_code == 422
    assert TooManyRequests().status_code == 429
    assert InternalServerError().status_code == 500
    assert ServiceUnavailable().status_code == 503


def test_subclasses_have_default_descriptions():
    assert NotFound().detail == "Not Found"
    assert Forbidden().detail == "Forbidden"
    assert InternalServerError().detail == "Internal Server Error"


def test_subclass_detail_override():
    exc = NotFound(detail="user gone")
    assert exc.status_code == 404
    assert exc.detail == "user gone"


def test_subclass_headers_pass_through():
    exc = Unauthorized(headers={"WWW-Authenticate": 'Bearer realm="api"'})
    assert exc.headers["WWW-Authenticate"] == 'Bearer realm="api"'


def test_subclasses_inherit_from_httpexception():
    # The contract: every spec exception is a `HTTPException`.
    for cls in (BadRequest, NotFound, Forbidden, InternalServerError):
        assert issubclass(cls, HTTPException)
    # `try: ... except HTTPException` catches every typed subclass.
    try:
        raise NotFound()
    except HTTPException as exc:
        assert isinstance(exc, NotFound)


def test_exception_for_status_returns_subclass():
    assert exception_for_status(404) is NotFound
    assert exception_for_status(403) is Forbidden
    assert exception_for_status(500) is InternalServerError
    # Unknown code → base class.
    assert exception_for_status(999) is HTTPException


def test_httpexception_base_requires_status_code():
    with pytest.raises(TypeError):
        HTTPException()  # no code, no subclass default


# ── abort() raises the right subclass ────────────────────────────────


def test_abort_raises_typed_subclass():
    with pytest.raises(NotFound) as info:
        abort(404)
    assert info.value.status_code == 404

    with pytest.raises(Forbidden):
        abort(403)

    with pytest.raises(TooManyRequests):
        abort(429, "slow down")


def test_abort_unknown_code_falls_back_to_base():
    with pytest.raises(HTTPException) as info:
        abort(499, "Client Closed")
    # Not one of the registered subclasses.
    assert type(info.value) is HTTPException
    assert info.value.status_code == 499


# ── MRO handler lookup ───────────────────────────────────────────────


def _req(path="/"):
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


async def test_handler_on_specific_subclass_catches_that_subclass():
    app = Veloce(debug=True, openapi_url=None)

    @app.exception_handler(NotFound)
    async def on_not_found(request, exc):

        return JSONResponse({"oops": "no such thing"}, status_code=404)

    @app.get("/")
    async def index():
        abort(404)

    resp = await app.handle_request(_req("/"))
    assert resp.status_code == 404
    assert b'"oops":"no such thing"' in resp.body


async def test_handler_on_base_class_catches_subclass():
    """Registering a handler against `HTTPException` should catch every
    typed subclass via the MRO walk — not just direct instances."""
    app = Veloce(debug=True, openapi_url=None)

    @app.exception_handler(HTTPException)
    async def on_http(request, exc):

        return JSONResponse(
            {"caught": type(exc).__name__, "code": exc.status_code},
            status_code=exc.status_code,
        )

    @app.get("/nf")
    async def nf():
        abort(404)

    @app.get("/fb")
    async def fb():
        abort(403)

    r1 = await app.handle_request(_req("/nf"))
    assert r1.status_code == 404
    assert b'"caught":"NotFound"' in r1.body

    r2 = await app.handle_request(_req("/fb"))
    assert r2.status_code == 403
    assert b'"caught":"Forbidden"' in r2.body


async def test_specific_handler_wins_over_base():
    """A `NotFound` handler should fire instead of an `HTTPException`
    handler when a `NotFound` is raised — the MRO walks specific first."""
    app = Veloce(debug=True, openapi_url=None)

    @app.exception_handler(HTTPException)
    async def on_http(request, exc):

        return JSONResponse({"by": "base"}, status_code=exc.status_code)

    @app.exception_handler(NotFound)
    async def on_nf(request, exc):

        return JSONResponse({"by": "specific"}, status_code=404)

    @app.get("/x")
    async def x():
        abort(404)

    resp = await app.handle_request(_req("/x"))
    assert resp.status_code == 404
    assert b'"by":"specific"' in resp.body


async def test_handler_for_user_exception_via_mro():
    """A handler on a user-defined base catches its subclasses."""
    app = Veloce(debug=True, openapi_url=None)

    class AppError(Exception):
        pass

    class UserNotFound(AppError):
        pass

    @app.exception_handler(AppError)
    async def on_app_err(request, exc):

        return JSONResponse(
            {"app_error": type(exc).__name__},
            status_code=500,
        )

    @app.get("/u")
    async def u():
        raise UserNotFound("nope")

    resp = await app.handle_request(_req("/u"))
    assert resp.status_code == 500
    assert b'"app_error":"UserNotFound"' in resp.body


async def test_handler_on_exception_catches_unhandled():
    """The existing fallback to `Exception` still works via the same MRO."""
    app = Veloce(debug=True, openapi_url=None)

    @app.exception_handler(Exception)
    async def fallback(request, exc):

        return JSONResponse({"fallback": str(exc)}, status_code=500)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    resp = await app.handle_request(_req("/boom"))
    assert resp.status_code == 500
    assert b'"fallback":"boom"' in resp.body
