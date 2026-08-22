"""`VeloceError` — the root every Veloce exception family shares.

Covers what the root buys (`except VeloceError` catches any family) and, just
as importantly, what it must not cost: every `except` clause that matched
before still matches, because the root was mixed in beside the existing bases
rather than replacing them.
"""

from __future__ import annotations

import pytest

import veloce
from veloce import (
    BadGateway,
    BadRequest,
    BadResetToken,
    BadSignature,
    BuildError,
    ConfigurationError,
    DuplicateRouteError,
    FilesKeyError,
    Forbidden,
    HTTPException,
    JSONResponse,
    JWTError,
    NotFound,
    Request,
    RequestValidationError,
    ServerNotImplemented,
    SetupError,
    TestClient,
    TooManyRequests,
    Unauthorized,
    ValidationError,
    Veloce,
    VeloceError,
    WebSocketDisconnect,
    WebSocketException,
)

# One representative per family the framework defines.
FAMILY_MEMBERS = [
    HTTPException(418, "teapot"),
    NotFound("gone"),
    ValidationError([{"loc": ["query", "n"], "msg": "bad", "type": "int_parsing"}]),
    RequestValidationError([]),
    WebSocketDisconnect(),
    WebSocketException(1008, "policy"),
    BuildError("endpoint", {}),
    DuplicateRouteError("/x", "GET", "a", "b"),
    SetupError("late"),
    ConfigurationError("ambiguous"),
    FilesKeyError("no field 'avatar'"),
    JWTError("bad token"),
    BadSignature("tampered"),
    BadResetToken("expired"),
]


@pytest.mark.parametrize("exc", FAMILY_MEMBERS, ids=lambda e: type(e).__name__)
def test_every_family_is_a_veloce_error(exc):
    assert isinstance(exc, VeloceError)


def test_except_veloce_error_catches_each_family():
    for exc in FAMILY_MEMBERS:
        try:
            raise exc
        except VeloceError as caught:
            assert caught is exc
        else:  # pragma: no cover - defensive
            pytest.fail(f"{type(exc).__name__} escaped except VeloceError")


def test_veloce_error_is_an_exception_not_a_base_exception():
    assert issubclass(VeloceError, Exception)
    # A bare `except Exception` in user code must still catch framework errors.
    with pytest.raises(Exception):
        raise NotFound()


# ── Back-compat: the stdlib bases that were already there stay there ──


@pytest.mark.parametrize(
    ("exc_cls", "args", "stdlib_base"),
    [
        (DuplicateRouteError, ("/x", "GET", "a", "b"), ValueError),
        (FilesKeyError, ("no field 'avatar'",), KeyError),
        (BuildError, ("endpoint", {}), LookupError),
        (SetupError, ("late",), RuntimeError),
        (ConfigurationError, ("ambiguous",), RuntimeError),
    ],
)
def test_stdlib_base_still_matches(exc_cls, args, stdlib_base):
    assert issubclass(exc_cls, stdlib_base)
    with pytest.raises(stdlib_base):
        raise exc_cls(*args)


def test_filekeyerror_message_survives_the_extra_base():
    # `FilesKeyError.__str__` bypasses `KeyError`'s repr-quoting; adding
    # `VeloceError` ahead of `KeyError` must not reinstate the quoting.
    assert str(FilesKeyError("no field 'avatar'")) == "no field 'avatar'"


def test_veloce_error_precedes_the_stdlib_base_in_the_mro():
    # Dispatch walks the MRO and takes the first registered handler, so a
    # handler on VeloceError must win over a broader stdlib handler.
    mro = DuplicateRouteError.__mro__
    assert mro.index(VeloceError) < mro.index(ValueError)


def test_http_subclass_relationships_are_unchanged():
    assert issubclass(NotFound, HTTPException)
    assert issubclass(RequestValidationError, ValidationError)
    assert issubclass(ValidationError, veloce.UnprocessableEntity)
    assert issubclass(veloce.WebSocketRequestValidationError, RequestValidationError)
    assert issubclass(veloce.BadTimeSignature, BadSignature)
    assert issubclass(veloce.InvalidTokenError, JWTError)


# ── Handler dispatch ──


def test_exception_handler_registered_on_veloce_error_catches_everything():
    app = Veloce()

    @app.exception_handler(VeloceError)
    async def on_any(request: Request, exc: VeloceError):
        return JSONResponse({"root": type(exc).__name__}, status_code=500)

    @app.get("/build")
    async def build():
        raise BuildError("nope", {})

    @app.get("/notfound")
    async def missing():
        raise NotFound("no such item")

    with TestClient(app) as client:
        assert client.get("/build").json() == {"root": "BuildError"}
        assert client.get("/notfound").json() == {"root": "NotFound"}


@pytest.mark.parametrize(
    ("exc_cls", "expected_status"),
    [
        (BadRequest, 400),
        (Unauthorized, 401),
        (Forbidden, 403),
        (NotFound, 404),
        (TooManyRequests, 429),
        (ServerNotImplemented, 501),
        (BadGateway, 502),
    ],
)
def test_raising_a_named_error_from_a_handler_produces_its_status(exc_cls, expected_status):
    app = Veloce()

    @app.get("/boom")
    async def boom():
        raise exc_cls("nope")

    with TestClient(app) as client:
        response = client.get("/boom")
    assert response.status_code == expected_status
    assert response.json()["detail"] == "nope"


def test_abort_still_raises_the_named_subclass():
    # `abort()` looks the class up by status code; the alias must not have
    # displaced the registration.
    assert veloce.exceptions.exception_for_status(404) is NotFound
    assert veloce.exceptions.exception_for_status(501) is ServerNotImplemented
