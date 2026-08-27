"""veloce.exception_handlers default handlers."""

from __future__ import annotations

import orjson
import pytest

from veloce import JSONResponse, Query, Request, ValidationError, Veloce
from veloce.exceptions import (
    HTTPException,
    NotFound,
    RequestValidationError,
    http_exception_handler,
    request_validation_exception_handler,
)


@pytest.mark.asyncio
async def test_http_exception_handler_renders_detail():
    resp = await http_exception_handler(None, HTTPException(404, "missing"))
    assert resp.status_code == 404
    assert orjson.loads(resp.body) == {"detail": "missing", "status_code": 404}


@pytest.mark.asyncio
async def test_http_exception_handler_uses_subclass_description():
    resp = await http_exception_handler(None, NotFound())
    assert resp.status_code == 404
    # Falls back to the subclass description when no detail given.
    assert "detail" in orjson.loads(resp.body)


@pytest.mark.asyncio
async def test_http_exception_handler_propagates_headers():
    exc = HTTPException(401, "no", headers={"WWW-Authenticate": "Basic"})
    resp = await http_exception_handler(None, exc)
    assert resp.headers["WWW-Authenticate"] == "Basic"


@pytest.mark.asyncio
async def test_request_validation_handler_renders_422():
    errors = [{"loc": ["query", "x"], "msg": "required", "type": "missing"}]
    resp = await request_validation_exception_handler(None, RequestValidationError(errors))
    assert resp.status_code == 422
    assert orjson.loads(resp.body) == {"detail": errors}


@pytest.mark.asyncio
async def test_request_validation_handler_empty_errors():
    resp = await request_validation_exception_handler(None, RequestValidationError([]))
    assert orjson.loads(resp.body) == {"detail": []}


# ── RequestValidationError is distinct from ValidationError ─────
#
# Moved here from `test_cookies_and_validation.py`, which bundled cookie parsing
# with validation-error hierarchy behaviour.


def test_request_validation_error_is_subclass_of_validation_error():
    """For back-compat: existing `except ValidationError` continues to
    catch application-level subclass."""
    assert issubclass(RequestValidationError, ValidationError)
    assert issubclass(RequestValidationError, HTTPException)
    assert RequestValidationError([{"loc": ["x"], "msg": "m", "type": "t"}]).status_code == 422


@pytest.mark.asyncio
async def test_resolver_raises_request_validation_error_specifically():
    """A missing required Query parameter must raise the new subclass —
    not the bare ValidationError."""
    app = Veloce(debug=True, openapi_url=None)

    captured: dict = {}

    @app.exception_handler(RequestValidationError)
    async def on_req_validation(request, exc):
        captured["exc_type"] = type(exc).__name__
        return JSONResponse({"req_validation": True}, status_code=422)

    @app.get("/items")
    async def items(q: str = Query()):  # no default => required
        return {"q": q}

    req = Request(method="GET", path="/items", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    assert resp.status_code == 422
    assert captured["exc_type"] == "RequestValidationError"
    assert b'"req_validation":true' in resp.body


@pytest.mark.asyncio
async def test_existing_validation_error_handler_still_catches():
    """A handler registered for ValidationError must still catch the new
    subclass via the MRO walk introduced in E3."""
    app = Veloce(debug=True, openapi_url=None)

    @app.exception_handler(ValidationError)
    async def on_val(request, exc):
        return JSONResponse(
            {"caught_via": "ValidationError", "subclass": type(exc).__name__},
            status_code=422,
        )

    @app.get("/x")
    async def x(q: str = Query()):  # required → triggers framework validation
        return {"q": q}

    req = Request(method="GET", path="/x", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    body = orjson.loads(resp.body)
    assert body["caught_via"] == "ValidationError"
    # The actual instance is a RequestValidationError under the hood.
    assert body["subclass"] == "RequestValidationError"


def test_request_validation_error_in_exports():
    """`from veloce import RequestValidationError` works."""
    from veloce import RequestValidationError as RVE_top  # noqa: N814

    assert RVE_top is RequestValidationError
